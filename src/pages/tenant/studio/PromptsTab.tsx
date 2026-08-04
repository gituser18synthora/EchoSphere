import { useEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import type {
  Prompt, PromptCompileResult, PromptTestResult, PromptType, PromptVariant,
  PromptVersion, StructuredPromptConfig, VoiceBot,
} from "@/types/domain";
import { useAsync } from "@/hooks/useAsync";
import {
  compilePromptPreview, createPrompt, deletePrompt, duplicatePrompt,
  listLanguages, listPrompts, savePromptVersion, testPrompt as runPromptTest, updatePrompt,
} from "@/services/api";
import {
  Button, Callout, CardSkeleton, ConfirmModal, Drawer, EmptyState, ErrorState,
  Field, MenuButton, Modal, StatusChip, Toggle,
} from "@/components/ui";
import { Icon } from "@/components/Icon";
import { useApp } from "@/state/AppContext";

/* ---------- Shared helpers ---------- */

interface Perms { canManage: boolean; canApprove: boolean; canPublish: boolean }
interface Lang { code: string; name: string; nativeName?: string | null }

type PatchFn = <K extends keyof StructuredPromptConfig>(
  key: K, value: Partial<NonNullable<StructuredPromptConfig[K]>>,
) => void;

const TYPE_OPTIONS: { value: PromptType; label: string }[] = [
  { value: "system", label: "Structured voice prompt" },
  { value: "greeting", label: "Greeting" },
  { value: "fallback", label: "Fallback" },
  { value: "escalation", label: "Escalation" },
  { value: "closing", label: "Closing" },
  { value: "reprompt", label: "Reprompt" },
  { value: "hold", label: "Hold" },
];
const typeLabel = (t: PromptType) => TYPE_OPTIONS.find((o) => o.value === t)?.label ?? t;

/* Two-level prompt choice for creation: both system flavors, then the simple types. */
type CreateKind = "system:structured" | "system:full" | Exclude<PromptType, "system">;
const CREATE_KIND_OPTIONS: { value: CreateKind; label: string }[] = [
  { value: "system:structured", label: "Structured voice prompt" },
  { value: "system:full", label: "Full / unified prompt" },
  ...TYPE_OPTIONS.filter((o): o is { value: Exclude<PromptType, "system">; label: string } => o.value !== "system"),
];

/* Starter for full / unified prompts — section headings the tenant fills in. */
const FULL_PROMPT_STARTER = `You are a voice assistant. Keep replies short, natural and speakable.

## Role & identity
## Objective
## Conversation flow
## Tone & language
## Business rules
## Intent handling
## Tools
## Objection handling
## Escalation
## Compliance
## Closing

Fill in each section above. Use placeholders like {customer_name} for caller
details — they resolve from the bot's runtime context on every call.`;

const SPECIAL_FIELDS: { key: string; label: string }[] = [
  { key: "silence", label: "Caller goes silent" },
  { key: "backgroundNoise", label: "Heavy background noise" },
  { key: "abusiveCaller", label: "Abusive caller" },
  { key: "emergency", label: "Emergency mentioned" },
  { key: "unsupportedRequest", label: "Unsupported request" },
  { key: "wrongNumber", label: "Wrong number" },
  { key: "voicemail", label: "Voicemail detected" },
  { key: "reconnect", label: "Caller reconnects" },
  { key: "providerFailure", label: "Provider failure" },
  { key: "longOperation", label: "Long-running operation" },
  { key: "repeatRequest", label: "Caller asks to repeat" },
  { key: "slowerSpeech", label: "Caller asks to slow down" },
  { key: "languageChange", label: "Caller switches language" },
];

/* Preview placeholder values only — variables resolve from live call context at runtime. */
const PREVIEW_VARS: Record<string, string> = {
  "{caller_name}": "Maria",
  "{clinic_name}": "your business",
  "{queue_wait}": "two minutes",
  "{appointment_date}": "Thursday at 10:15 AM",
};

/* Client-side placeholder detection for the full-prompt editor — same grammar
   the runtime resolver uses ({{key}} and {key}), keys normalized alike. */
const PLACEHOLDER_RE = /\{\{\s*([^{}\n]{1,40}?)\s*\}\}|\{\s*([^{}\n]{1,40}?)\s*\}/g;
const normalizeVarKey = (raw: string) =>
  raw.trim().toLowerCase().replace(/[^a-z0-9ऀ-ॿ]+/g, "_").replace(/^_+|_+$/g, "");
function detectVariables(text: string): string[] {
  const seen = new Set<string>();
  for (const m of text.matchAll(PLACEHOLDER_RE)) {
    const key = normalizeVarKey(m[1] ?? m[2] ?? "");
    if (key) seen.add(key);
  }
  return [...seen];
}

const errMsg = (e: unknown, fallback: string) => (e instanceof Error && e.message ? e.message : fallback);
const fmtDate = (iso?: string | null) =>
  iso ? new Date(iso).toLocaleDateString("en-US", { month: "short", day: "numeric" }) : "—";
const fmtDateTime = (iso?: string | null) =>
  iso ? new Date(iso).toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "—";

const preStyle: CSSProperties = {
  margin: 0, padding: 12, background: "var(--surface-2)", borderRadius: 10,
  fontSize: 12, lineHeight: 1.55, whiteSpace: "pre-wrap", wordBreak: "break-word",
  maxHeight: 320, overflow: "auto",
  fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
};

/* ---------- Main tab ---------- */

export default function PromptsTab({ bot }: { bot: VoiceBot }) {
  const { toast, hasPermission } = useApp();
  const legacy = hasPermission("prompts.manage");
  const perms: Perms = {
    canManage: legacy || hasPermission("manage_prompts"),
    canApprove: legacy || hasPermission("approve_prompts"),
    canPublish: legacy || hasPermission("publish_prompts"),
  };

  const q = useAsync(() => listPrompts(bot.id), [bot.id]);
  const langsQ = useAsync(() => listLanguages(), []);
  const languages: Lang[] = langsQ.data ?? [];

  const [createOpen, setCreateOpen] = useState(false);
  const [openPrompt, setOpenPrompt] = useState<Prompt | null>(null);
  const [testTarget, setTestTarget] = useState<Prompt | null>(null);
  const [archiveTarget, setArchiveTarget] = useState<Prompt | null>(null);
  const [archiving, setArchiving] = useState(false);

  const prompts = q.data ?? [];

  const duplicate = async (p: Prompt) => {
    try {
      const copy = await duplicatePrompt(p.id);
      toast(`“${p.name}” duplicated as “${copy.name}” (draft)`);
      q.reload();
    } catch (e) {
      toast(errMsg(e, "Duplicate failed"), "error");
    }
  };

  const archive = async () => {
    if (!archiveTarget) return;
    setArchiving(true);
    try {
      await deletePrompt(archiveTarget.id);
      toast(`“${archiveTarget.name}” archived`);
      setArchiveTarget(null);
      if (openPrompt?.id === archiveTarget.id) setOpenPrompt(null);
      q.reload();
    } catch (e) {
      toast(errMsg(e, "Archive failed"), "error");
    } finally {
      setArchiving(false);
    }
  };

  return (
    <div className="col gap-16">
      <div className="row-between wrap gap-8">
        <div className="col" style={{ gap: 2 }}>
          <span className="t-section" style={{ fontSize: 15 }}>Prompts</span>
          <span className="t-micro">
            Structured system prompts and business messages for “{bot.name}”. Platform safety preambles stay managed centrally.
          </span>
        </div>
        {perms.canManage && (
          <Button variant="primary" icon="plus" onClick={() => setCreateOpen(true)}>New prompt</Button>
        )}
      </div>

      {q.loading && (
        <div className="grid grid-2">
          {Array.from({ length: 4 }).map((_, i) => <CardSkeleton key={i} rows={3} />)}
        </div>
      )}
      {!q.loading && q.error && <ErrorState message={q.error} onRetry={q.reload} />}

      {!q.loading && !q.error && prompts.length === 0 && (
        <EmptyState
          icon="message"
          title="No prompts yet"
          body={perms.canManage
            ? "Create a structured voice prompt to define how the assistant behaves, or a greeting — the first thing every caller hears."
            : "No prompts have been created for this bot yet."}
          action={perms.canManage
            ? <Button variant="primary" icon="plus" onClick={() => setCreateOpen(true)}>New prompt</Button>
            : undefined}
        />
      )}

      {prompts.length > 0 && (
        <div className="grid grid-2">
          {prompts.map((p) => {
            const latest = p.versions[0];
            const active = p.versions.find((v) => v.version === p.activeVersion) ?? latest;
            const isFullMode = latest?.promptMode === "full";
            const snippet = p.type === "system"
              ? (p.description || active?.compiledPrompt || (isFullMode
                  ? "Full / unified prompt — open the editor to write it."
                  : "Structured voice prompt — open the builder to configure sections."))
              : (active?.variants[0]?.content || p.description || "No content yet — open the editor to write it.");
            return (
              <div
                key={p.id}
                className="card card-pad card-clickable col gap-10"
                style={{ cursor: "pointer" }}
                onClick={() => setOpenPrompt(p)}
              >
                <div className="row-between gap-8">
                  <span className="row gap-8" style={{ minWidth: 0 }}>
                    <span className="tag" style={{ textTransform: "capitalize", flexShrink: 0 }}>
                      {p.type === "system" ? (isFullMode ? "full" : "structured") : p.type}
                    </span>
                    <span className="t-strong truncate" style={{ fontSize: 13.5 }}>{p.name}</span>
                  </span>
                  <span className="row gap-6" style={{ flexShrink: 0 }}>
                    <StatusChip status={p.state} />
                    <MenuButton
                      label={`Actions for ${p.name}`}
                      actions={[
                        { label: perms.canManage ? "Edit" : "View", icon: "edit", onClick: () => setOpenPrompt(p) },
                        { label: "Test", icon: "play", onClick: () => setTestTarget(p) },
                        { label: "Duplicate", icon: "copy", disabled: !perms.canManage, onClick: () => void duplicate(p) },
                        "sep",
                        {
                          label: "Archive", icon: "trash", danger: true,
                          disabled: !perms.canManage || p.state === "archived",
                          onClick: () => setArchiveTarget(p),
                        },
                      ]}
                    />
                  </span>
                </div>
                <p
                  className="t-sub"
                  style={{
                    fontSize: 12.5, lineHeight: 1.55, display: "-webkit-box",
                    WebkitLineClamp: 2, WebkitBoxOrient: "vertical", overflow: "hidden", margin: 0,
                  }}
                >
                  {snippet}
                </p>
                <div className="row-between t-micro wrap gap-8">
                  <span className="row gap-8 wrap">
                    <span className="row gap-4">
                      <Icon name="version" size={12} />
                      v{p.activeVersion} active{p.publishedVersion != null ? ` · v${p.publishedVersion} published` : ""}
                    </span>
                    {p.type !== "system" && active && (
                      <span className="row gap-4">
                        <Icon name="globe" size={12} />
                        {active.variants.length} language{active.variants.length === 1 ? "" : "s"}
                      </span>
                    )}
                  </span>
                  {latest && <span>Updated {fmtDate(latest.editedAt)} by {latest.editedBy}</span>}
                </div>
              </div>
            );
          })}
        </div>
      )}

      <CreatePromptModal
        open={createOpen}
        bot={bot}
        onClose={() => setCreateOpen(false)}
        onCreated={(p) => { setCreateOpen(false); q.reload(); setOpenPrompt(p); }}
      />

      {openPrompt && (
        <PromptDrawer
          key={`${openPrompt.id}:${openPrompt.versions[0]?.version ?? 0}:${openPrompt.state}:${openPrompt.activeVersion}:${openPrompt.name}`}
          prompt={openPrompt}
          languages={languages}
          perms={perms}
          onClose={() => setOpenPrompt(null)}
          onUpdated={(p) => { setOpenPrompt(p); q.reload(); }}
          onOpenTest={() => setTestTarget(openPrompt)}
        />
      )}

      {testTarget && (
        <TestDrawer prompt={testTarget} languages={languages} onClose={() => setTestTarget(null)} />
      )}

      <ConfirmModal
        open={!!archiveTarget}
        onClose={() => setArchiveTarget(null)}
        onConfirm={() => void archive()}
        danger
        busy={archiving}
        title="Archive prompt?"
        confirmLabel="Archive"
        body={<>“{archiveTarget?.name}” will stop being available to the bot. Version history is kept and the prompt can be restored by an administrator.</>}
      />
    </div>
  );
}

/* ---------- Creation modal ---------- */

function CreatePromptModal({ open, bot, onClose, onCreated }: {
  open: boolean; bot: VoiceBot; onClose: () => void; onCreated: (p: Prompt) => void;
}) {
  const { toast } = useApp();
  const [name, setName] = useState("");
  const [kind, setKind] = useState<CreateKind>("system:structured");
  const [description, setDescription] = useState("");
  const [nameErr, setNameErr] = useState("");
  const [busy, setBusy] = useState(false);

  const isSystemKind = kind.startsWith("system:");
  const kindHint = kind === "system:structured"
    ? "Multi-section builder — identity, tone, knowledge rules, handoff and more, compiled deterministically."
    : kind === "system:full"
      ? "One free-form prompt you write end to end — {variables} resolve from the runtime context on every call."
      : "Single spoken message with per-language variants.";

  const submit = async () => {
    if (!name.trim()) { setNameErr("Give the prompt a name"); return; }
    setBusy(true);
    try {
      const base = { name: name.trim(), description: description.trim() || undefined };
      const created = kind === "system:structured"
        ? await createPrompt(bot.id, {
            ...base, type: "system",
            structuredConfig: { identity: { botName: bot.name, role: "voice assistant" } },
          })
        : kind === "system:full"
          ? await createPrompt(bot.id, {
              ...base, type: "system", promptMode: "full", fullPrompt: FULL_PROMPT_STARTER,
            })
          : await createPrompt(bot.id, {
              ...base, type: kind,
              variants: [{ language: "en-US", content: "" }],
            });
      toast(`“${created.name}” created as a draft`);
      setName(""); setDescription(""); setKind("system:structured"); setNameErr("");
      onCreated(created);
    } catch (e) {
      toast(errMsg(e, "Could not create the prompt"), "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="New prompt"
      sub="Prompts start as drafts and go live after approval and publishing."
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button variant="primary" icon="plus" busy={busy} onClick={() => void submit()}>
            {kind === "system:structured" ? "Create & open builder" : "Create & open editor"}
          </Button>
        </>
      }
    >
      <div className="col gap-12">
        <Field label="Name" required error={nameErr}>
          <input
            className="input"
            value={name}
            placeholder={isSystemKind ? "Main system prompt" : "Business-hours greeting"}
            aria-invalid={!!nameErr}
            onChange={(e) => { setName(e.target.value); setNameErr(""); }}
          />
        </Field>
        <Field label="Prompt type" hint={kindHint}>
          <select className="select" value={kind} onChange={(e) => setKind(e.target.value as CreateKind)}>
            {CREATE_KIND_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
          </select>
        </Field>
        <Field label="Description" hint="Optional — helps teammates find the right prompt.">
          <textarea
            className="textarea"
            rows={2}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
        </Field>
      </div>
    </Modal>
  );
}

/* ---------- Prompt drawer (builder for system, editor for simple types) ---------- */

function PromptDrawer({ prompt, languages, perms, onClose, onUpdated, onOpenTest }: {
  prompt: Prompt; languages: Lang[]; perms: Perms;
  onClose: () => void; onUpdated: (p: Prompt) => void; onOpenTest: () => void;
}) {
  const { toast } = useApp();
  const isSystem = prompt.type === "system";
  const latest = prompt.versions[0];
  const isFull = isSystem && latest?.promptMode === "full";

  const [name, setName] = useState(prompt.name);
  const [cfg, setCfg] = useState<StructuredPromptConfig>(
    () => JSON.parse(JSON.stringify(latest?.structuredConfig ?? {})) as StructuredPromptConfig,
  );
  const [fullPrompt, setFullPrompt] = useState(
    () => latest?.fullPrompt ?? latest?.compiledPrompt ?? "",
  );
  const [variants, setVariants] = useState<PromptVariant[]>(
    () => (latest?.variants ?? []).map((v) => ({ ...v })),
  );
  const [disallowedText, setDisallowedText] = useState(
    () => (latest?.structuredConfig?.safety?.disallowed ?? []).join(", "),
  );
  const [note, setNote] = useState("");
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [actionErr, setActionErr] = useState<string | null>(null);
  const [confirmClose, setConfirmClose] = useState(false);
  const [rollbackTo, setRollbackTo] = useState<number | null>(null);

  const patch: PatchFn = (key, value) => {
    setDirty(true);
    setCfg((c) => {
      const section = { ...((c[key] ?? {}) as Record<string, unknown>), ...(value as Record<string, unknown>) };
      return { ...c, [key]: section } as StructuredPromptConfig;
    });
  };

  const onDisallowedText = (v: string) => {
    setDisallowedText(v);
    patch("safety", { disallowed: v.split(",").map((s) => s.trim()).filter(Boolean) });
  };

  const changeVariants = (next: PromptVariant[]) => { setDirty(true); setVariants(next); };
  const changeName = (v: string) => { setDirty(true); setName(v); };
  const changeFullPrompt = (v: string) => { setDirty(true); setFullPrompt(v); };

  const save = async (submit: boolean) => {
    setBusy(true);
    setActionErr(null);
    try {
      if (name.trim() && name.trim() !== prompt.name) {
        await updatePrompt(prompt.id, { name: name.trim() });
      }
      const updated = await savePromptVersion(prompt.id, {
        note: note.trim() || undefined,
        ...(isSystem
          ? (isFull
              ? { promptMode: "full" as const, fullPrompt }
              : { promptMode: "structured" as const, structuredConfig: cfg })
          : { variants }),
        submitForApproval: submit,
      });
      toast(submit
        ? `Saved v${updated.versions[0]?.version ?? ""} and submitted for approval`
        : `Draft v${updated.versions[0]?.version ?? ""} saved`);
      onUpdated(updated);
    } catch (e) {
      setActionErr(errMsg(e, "Save failed"));
    } finally {
      setBusy(false);
    }
  };

  const transition = async (state: string, done: string) => {
    setBusy(true);
    setActionErr(null);
    try {
      const updated = await updatePrompt(prompt.id, { state });
      toast(done);
      onUpdated(updated);
    } catch (e) {
      setActionErr(errMsg(e, "Action failed"));
    } finally {
      setBusy(false);
    }
  };

  const doRollback = async () => {
    if (rollbackTo == null) return;
    setBusy(true);
    setActionErr(null);
    try {
      const updated = await updatePrompt(prompt.id, { activeVersion: rollbackTo });
      toast(`Active version set to v${rollbackTo}`);
      setRollbackTo(null);
      onUpdated(updated);
    } catch (e) {
      setActionErr(errMsg(e, "Rollback failed"));
      setRollbackTo(null);
    } finally {
      setBusy(false);
    }
  };

  const restore = async (ver: PromptVersion) => {
    setBusy(true);
    setActionErr(null);
    try {
      const updated = await savePromptVersion(prompt.id, {
        note: `Restored from v${ver.version}`,
        ...(isSystem
          ? (ver.promptMode === "full"
              ? { promptMode: "full" as const, fullPrompt: ver.fullPrompt ?? ver.compiledPrompt ?? "" }
              : { promptMode: "structured" as const, structuredConfig: ver.structuredConfig ?? {} })
          : { variants: ver.variants }),
        submitForApproval: false,
      });
      toast(`v${ver.version} restored as a new draft`);
      onUpdated(updated);
    } catch (e) {
      setActionErr(errMsg(e, "Restore failed"));
    } finally {
      setBusy(false);
    }
  };

  const requestClose = () => { if (dirty) setConfirmClose(true); else onClose(); };

  const lifecycleMeta: string[] = [];
  if (prompt.approvedBy) {
    lifecycleMeta.push(`Approved by ${prompt.approvedBy}${prompt.approvedAt ? ` on ${fmtDateTime(prompt.approvedAt)}` : ""}`);
  }
  if (prompt.publishedAt) {
    lifecycleMeta.push(`Published ${fmtDateTime(prompt.publishedAt)}${prompt.publishedVersion != null ? ` (v${prompt.publishedVersion})` : ""}`);
  }

  return (
    <Drawer
      open
      onClose={requestClose}
      wide
      title={<span className="row gap-8">{prompt.name}<StatusChip status={prompt.state} /></span>}
      sub={`${isFull ? "Full / unified prompt" : typeLabel(prompt.type)}${prompt.description ? ` · ${prompt.description}` : ""}`}
      headerExtra={<Button size="sm" variant="ghost" icon="play" onClick={onOpenTest}>Test</Button>}
      footer={
        perms.canManage ? (
          <>
            <input
              className="input"
              placeholder="Version note (optional)"
              aria-label="Version note"
              value={note}
              style={{ flex: 1, minWidth: 140 }}
              onChange={(e) => setNote(e.target.value)}
            />
            <Button
              icon="check"
              busy={busy}
              disabled={!dirty}
              title={dirty ? undefined : "No unsaved changes"}
              onClick={() => void save(false)}
            >
              Save draft
            </Button>
            <Button
              variant="primary"
              icon="send"
              busy={busy}
              disabled={!dirty}
              title={dirty ? undefined : "No unsaved changes — use “Submit for approval” above"}
              onClick={() => void save(true)}
            >
              Save & submit for approval
            </Button>
          </>
        ) : (
          <span className="t-micro grow">You need the “manage prompts” permission to edit and save this prompt.</span>
        )
      }
    >
      <div className="col gap-16">
        {actionErr && <Callout tone="critical" title="Action failed">{actionErr}</Callout>}

        {/* Lifecycle */}
        <div className="card card-pad col gap-8">
          <div className="row-between wrap gap-8">
            <span className="row gap-8 wrap">
              <StatusChip status={prompt.state} />
              <span className="t-micro t-num">
                v{prompt.activeVersion} active · v{latest?.version ?? "—"} latest
                {prompt.publishedVersion != null ? ` · v${prompt.publishedVersion} published` : ""}
              </span>
            </span>
            <span className="row gap-6 wrap">
              {(prompt.state === "draft" || prompt.state === "rejected") && perms.canManage && (
                <Button
                  size="sm" icon="send" disabled={busy || dirty}
                  title={dirty ? "Save your changes first" : undefined}
                  onClick={() => void transition("pending_approval", "Submitted for approval")}
                >
                  Submit for approval
                </Button>
              )}
              {prompt.state === "pending_approval" && perms.canApprove && (
                <>
                  <Button
                    size="sm" variant="danger-ghost" icon="x" disabled={busy || dirty}
                    title={dirty ? "Save or discard your changes first" : undefined}
                    onClick={() => void transition("rejected", "Prompt rejected — the author can revise and resubmit")}
                  >
                    Reject
                  </Button>
                  <Button
                    size="sm" variant="primary" icon="check" disabled={busy || dirty}
                    title={dirty ? "Save or discard your changes first" : undefined}
                    onClick={() => void transition("approved", "Prompt approved")}
                  >
                    Approve
                  </Button>
                </>
              )}
              {prompt.state === "pending_approval" && !perms.canApprove && (
                <span className="t-micro">Waiting for an approver</span>
              )}
              {prompt.state === "approved" && perms.canPublish && (
                <Button
                  size="sm" variant="primary" icon="rocket" disabled={busy || dirty}
                  title={dirty ? "Save or discard your changes first" : undefined}
                  onClick={() => void transition("published", "Prompt published — live for new calls")}
                >
                  Publish
                </Button>
              )}
              {prompt.state === "approved" && !perms.canPublish && (
                <span className="t-micro">Approved — waiting to be published</span>
              )}
            </span>
          </div>
          {lifecycleMeta.length > 0 && <span className="t-micro">{lifecycleMeta.join(" · ")}</span>}
        </div>

        {isFull ? (
          <>
            <div className="grid grid-2" style={{ gap: 12 }}>
              <Field label="Prompt name" required>
                <input className="input" value={name} onChange={(e) => changeName(e.target.value)} />
              </Field>
              <Field label="Description" hint="Set when the prompt is created">
                <input className="input" value={prompt.description ?? ""} disabled />
              </Field>
            </div>
            <FullPromptEditor value={fullPrompt} onChange={changeFullPrompt} />
            <FullPromptPreview fullPrompt={fullPrompt} />
          </>
        ) : isSystem ? (
          <>
            <SystemSections
              cfg={cfg}
              patch={patch}
              name={name}
              onName={changeName}
              description={prompt.description}
              disallowedText={disallowedText}
              onDisallowedText={onDisallowedText}
            />
            <CompilePreview cfg={cfg} />
          </>
        ) : (
          <>
            <div className="grid grid-2" style={{ gap: 12 }}>
              <Field label="Prompt name" required>
                <input className="input" value={name} onChange={(e) => changeName(e.target.value)} />
              </Field>
              <Field label="Description" hint="Set when the prompt is created">
                <input className="input" value={prompt.description ?? ""} disabled />
              </Field>
            </div>
            <SimpleEditor
              variants={variants}
              variables={prompt.variables}
              languages={languages}
              onChange={changeVariants}
            />
          </>
        )}

        <VersionHistory
          prompt={prompt}
          isSystem={isSystem}
          perms={perms}
          busy={busy}
          dirty={dirty}
          onRollback={(v) => setRollbackTo(v)}
          onRestore={(ver) => void restore(ver)}
        />
      </div>

      <ConfirmModal
        open={confirmClose}
        onClose={() => setConfirmClose(false)}
        onConfirm={onClose}
        danger
        title="Discard unsaved changes?"
        confirmLabel="Discard changes"
        body="You have edits that haven’t been saved. Closing the panel will discard them."
      />
      <ConfirmModal
        open={rollbackTo != null}
        onClose={() => setRollbackTo(null)}
        onConfirm={() => void doRollback()}
        busy={busy}
        danger={prompt.state === "published"}
        title={`Make v${rollbackTo ?? ""} the active version?`}
        confirmLabel="Change active version"
        body={prompt.state === "published"
          ? "This prompt is published — moving the active pointer changes what live calls use immediately."
          : `The active pointer will move to v${rollbackTo ?? ""}. No new version is created.`}
      />
    </Drawer>
  );
}

/* ---------- Structured builder sections ---------- */

function SystemSections({ cfg, patch, name, onName, description, disallowedText, onDisallowedText }: {
  cfg: StructuredPromptConfig; patch: PatchFn;
  name: string; onName: (v: string) => void; description?: string;
  disallowedText: string; onDisallowedText: (v: string) => void;
}) {
  const id = cfg.identity ?? {};
  const cs = cfg.conversationStart ?? {};
  const bh = cfg.behavior ?? {};
  const kn = cfg.knowledge ?? {};
  const rc = cfg.recovery ?? {};
  const sf = cfg.safety ?? {};
  const hf = cfg.handoff ?? {};
  const cl = cfg.closing ?? {};
  const sp = cfg.special ?? {};
  const adv = cfg.advanced ?? {};

  return (
    <>
      <Sec title="Overview" hint="Who the assistant is and what it may talk about" defaultOpen>
        <div className="grid grid-2" style={{ gap: 12 }}>
          <Field label="Prompt name" required>
            <input className="input" value={name} onChange={(e) => onName(e.target.value)} />
          </Field>
          <Field label="Description" hint="Set when the prompt is created">
            <input className="input" value={description ?? ""} disabled />
          </Field>
          <TextField label="Bot name" required value={id.botName} placeholder="Ava"
            onChange={(v) => patch("identity", { botName: v })} />
          <TextField label="Organization" value={id.organizationName} placeholder="Acme Clinics"
            onChange={(v) => patch("identity", { organizationName: v })} />
          <TextField label="Role" required value={id.role} placeholder="voice assistant"
            onChange={(v) => patch("identity", { role: v })} />
          <TextField label="Sector" value={id.sector} placeholder="healthcare"
            onChange={(v) => patch("identity", { sector: v })} />
          <TextField label="Responsibility" value={id.responsibility} placeholder="Book, move and cancel appointments"
            onChange={(v) => patch("identity", { responsibility: v })} />
          <TextField label="Allowed scope" value={id.allowedScope} placeholder="Only topics related to the clinic"
            onChange={(v) => patch("identity", { allowedScope: v })} />
        </div>
      </Sec>

      <Sec title="Conversation start" hint="Greetings, consent and verification">
        <div className="grid grid-2" style={{ gap: 12 }}>
          <TextField label="Initial greeting" value={cs.initialGreeting}
            onChange={(v) => patch("conversationStart", { initialGreeting: v })} />
          <TextField label="Inbound greeting" value={cs.inboundGreeting}
            onChange={(v) => patch("conversationStart", { inboundGreeting: v })} />
          <TextField label="Outbound greeting" value={cs.outboundGreeting}
            onChange={(v) => patch("conversationStart", { outboundGreeting: v })} />
          <TextField label="After-hours greeting" value={cs.afterHoursGreeting}
            onChange={(v) => patch("conversationStart", { afterHoursGreeting: v })} />
          <TextField label="Recording consent" value={cs.recordingConsent}
            onChange={(v) => patch("conversationStart", { recordingConsent: v })} />
          <TextField label="Language selection" value={cs.languageSelection}
            onChange={(v) => patch("conversationStart", { languageSelection: v })} />
          <TextField label="Identity verification" value={cs.identityVerification}
            onChange={(v) => patch("conversationStart", { identityVerification: v })} />
        </div>
        <ToggleRow label="Ask the reason for the call" hint="Prompt the caller for their intent right after the greeting"
          checked={cs.reasonForCall ?? false} onChange={(v) => patch("conversationStart", { reasonForCall: v })} />
      </Sec>

      <Sec title="Voice & tone" hint="How the assistant speaks">
        <div className="grid grid-2" style={{ gap: 12 }}>
          <SelectField label="Tone" value={bh.tone}
            options={["friendly", "professional", "warm", "formal", "empathetic", "neutral"]}
            onChange={(v) => patch("behavior", { tone: v })} />
          <SelectField label="Response length" value={bh.responseLength}
            options={["short", "medium", "detailed"]}
            onChange={(v) => patch("behavior", { responseLength: v })} />
          <SelectField label="Empathy" value={bh.empathy}
            options={["low", "medium", "high"]}
            onChange={(v) => patch("behavior", { empathy: v })} />
          <TextField label="Formality" value={bh.formality} placeholder="Polite, first-name basis"
            onChange={(v) => patch("behavior", { formality: v })} />
          <TextField label="Style" value={bh.style} placeholder="Concise, no jargon"
            onChange={(v) => patch("behavior", { style: v })} />
          <TextField label="Pronunciation" value={bh.pronunciation} placeholder="EchoSphere → “echo-sphere”"
            onChange={(v) => patch("behavior", { pronunciation: v })} />
          <TextField label="Number reading" value={bh.numberReading} placeholder="Digit by digit for account numbers"
            onChange={(v) => patch("behavior", { numberReading: v })} />
          <TextField label="Date reading" value={bh.dateReading} placeholder="“March third”, not “3/3”"
            onChange={(v) => patch("behavior", { dateReading: v })} />
          <TextField label="Currency reading" value={bh.currencyReading} placeholder="“twelve dollars fifty”"
            onChange={(v) => patch("behavior", { currencyReading: v })} />
        </div>
        <ToggleRow label="Confirm before actions" hint="Read back and confirm before state-changing operations"
          checked={bh.confirmBeforeActions ?? false} onChange={(v) => patch("behavior", { confirmBeforeActions: v })} />
        <ToggleRow label="Use the customer’s name"
          checked={bh.useCustomerName ?? false} onChange={(v) => patch("behavior", { useCustomerName: v })} />
      </Sec>

      <Sec title="Knowledge rules" hint="When and how indexed sources are used">
        <ToggleRow label="Use knowledge base" hint="Answer factual questions from indexed sources"
          checked={kn.useKb ?? true} onChange={(v) => patch("knowledge", { useKb: v })} />
        <div className="grid grid-2" style={{ gap: 12 }}>
          <TextField label="When to use" value={kn.whenToUse} placeholder="For any factual question about services or policies"
            onChange={(v) => patch("knowledge", { whenToUse: v })} />
          <TextField label="No-answer behavior" value={kn.noAnswerBehavior} placeholder="Say you don’t know and offer a human"
            onChange={(v) => patch("knowledge", { noAnswerBehavior: v })} />
        </div>
        <ToggleRow label="Cite sources"
          checked={kn.citeSources ?? false} onChange={(v) => patch("knowledge", { citeSources: v })} />
        <ToggleRow label="Ask a clarifying question when unsure"
          checked={kn.askClarification ?? false} onChange={(v) => patch("knowledge", { askClarification: v })} />
        <ToggleRow label="Transfer when no answer is found"
          checked={kn.transferOnNoAnswer ?? false} onChange={(v) => patch("knowledge", { transferOnNoAnswer: v })} />
      </Sec>

      <Sec title="Confusion recovery" hint="What happens when the assistant can’t follow the caller">
        <div className="grid grid-2" style={{ gap: 12 }}>
          <TextField label="First clarification" value={rc.firstClarification} placeholder="Sorry — could you say that again?"
            onChange={(v) => patch("recovery", { firstClarification: v })} />
          <TextField label="Second clarification" value={rc.secondClarification} placeholder="I still didn’t catch that. Could you rephrase?"
            onChange={(v) => patch("recovery", { secondClarification: v })} />
          <TextField label="Repeat request" value={rc.repeatRequest} placeholder="What to say when asked to repeat"
            onChange={(v) => patch("recovery", { repeatRequest: v })} />
          <TextField label="Rephrase strategy" value={rc.rephraseStrategy} placeholder="Use simpler words the second time"
            onChange={(v) => patch("recovery", { rephraseStrategy: v })} />
          <TextField label="Fallback message" value={rc.fallbackMessage} placeholder="Let me connect you with a colleague."
            onChange={(v) => patch("recovery", { fallbackMessage: v })} />
          <TextField label="Low STT-confidence behavior" value={rc.lowSttConfidenceBehavior} placeholder="Confirm what was heard before acting"
            onChange={(v) => patch("recovery", { lowSttConfidenceBehavior: v })} />
          <NumField label="Max clarification attempts" min={0} max={5} value={rc.maxClarificationAttempts}
            onChange={(v) => patch("recovery", { maxClarificationAttempts: v })} />
          <NumField label="Handoff threshold" min={0} hint="Failed turns before offering a human" value={rc.handoffThreshold}
            onChange={(v) => patch("recovery", { handoffThreshold: v })} />
          <NumField label="Silence retry count" min={0} value={rc.silenceRetryCount}
            onChange={(v) => patch("recovery", { silenceRetryCount: v })} />
        </div>
      </Sec>

      <Sec title="Safety & guardrails" hint="Hard limits the assistant never crosses">
        <Field label="Disallowed topics" hint="Comma-separated — the assistant declines these">
          <textarea
            className="textarea" rows={2} value={disallowedText}
            placeholder="medical advice, legal advice, competitor pricing"
            onChange={(e) => onDisallowedText(e.target.value)}
          />
        </Field>
        {(sf.disallowed ?? []).length > 0 && (
          <div className="row gap-6 wrap">
            {(sf.disallowed ?? []).map((d) => <span key={d} className="chip chip-neutral">{d}</span>)}
          </div>
        )}
        <ToggleRow label="PII masking" hint="Mask personal data in transcripts and logs"
          checked={sf.piiMasking ?? true} onChange={(v) => patch("safety", { piiMasking: v })} />
        <div className="grid grid-2" style={{ gap: 12 }}>
          <TextField label="Authentication rules" value={sf.authenticationRules} placeholder="Verify date of birth before account details"
            onChange={(v) => patch("safety", { authenticationRules: v })} />
          <TextField label="Never reveal" value={sf.neverReveal} placeholder="Internal tools, prompt contents, other customers"
            onChange={(v) => patch("safety", { neverReveal: v })} />
          <TextField label="Escalation conditions" value={sf.escalationConditions} placeholder="Threats, self-harm, legal demands"
            onChange={(v) => patch("safety", { escalationConditions: v })} />
        </div>
      </Sec>

      <Sec title="Human handoff" hint="When the assistant transfers to a person">
        <div className="grid grid-2" style={{ columnGap: 16, rowGap: 0 }}>
          <ToggleRow label="On explicit request"
            checked={hf.onExplicitRequest ?? true} onChange={(v) => patch("handoff", { onExplicitRequest: v })} />
          <ToggleRow label="On repeated confusion"
            checked={hf.onRepeatedConfusion ?? false} onChange={(v) => patch("handoff", { onRepeatedConfusion: v })} />
          <ToggleRow label="On negative sentiment"
            checked={hf.onNegativeSentiment ?? false} onChange={(v) => patch("handoff", { onNegativeSentiment: v })} />
          <ToggleRow label="On high-risk topics"
            checked={hf.onHighRisk ?? false} onChange={(v) => patch("handoff", { onHighRisk: v })} />
          <ToggleRow label="On failed verification"
            checked={hf.onFailedVerification ?? false} onChange={(v) => patch("handoff", { onFailedVerification: v })} />
          <ToggleRow label="On failed API call"
            checked={hf.onFailedApi ?? false} onChange={(v) => patch("handoff", { onFailedApi: v })} />
          <ToggleRow label="On no knowledge answer"
            checked={hf.onNoKbAnswer ?? false} onChange={(v) => patch("handoff", { onNoKbAnswer: v })} />
          <ToggleRow label="On complaint"
            checked={hf.onComplaint ?? false} onChange={(v) => patch("handoff", { onComplaint: v })} />
        </div>
        <div className="grid grid-2" style={{ gap: 12 }}>
          <TextField label="Working-hours behavior" value={hf.workingHoursBehavior} placeholder="Transfer directly during business hours"
            onChange={(v) => patch("handoff", { workingHoursBehavior: v })} />
          <TextField label="Queue-unavailable behavior" value={hf.queueUnavailableBehavior} placeholder="Offer a callback and log a ticket"
            onChange={(v) => patch("handoff", { queueUnavailableBehavior: v })} />
        </div>
      </Sec>

      <Sec title="Conversation end" hint="How calls wrap up">
        <div className="grid grid-2" style={{ columnGap: 16, rowGap: 0 }}>
          <ToggleRow label="Confirm resolution"
            checked={cl.confirmResolution ?? false} onChange={(v) => patch("closing", { confirmResolution: v })} />
          <ToggleRow label="Summarize actions"
            checked={cl.summarizeActions ?? false} onChange={(v) => patch("closing", { summarizeActions: v })} />
          <ToggleRow label="Mention reference number"
            checked={cl.mentionReference ?? false} onChange={(v) => patch("closing", { mentionReference: v })} />
          <ToggleRow label="Ask “anything else?”"
            checked={cl.askAnythingElse ?? false} onChange={(v) => patch("closing", { askAnythingElse: v })} />
        </div>
        <div className="grid grid-2" style={{ gap: 12 }}>
          <TextField label="Closing message" value={cl.closingMessage} placeholder="Thanks for calling — have a great day!"
            onChange={(v) => patch("closing", { closingMessage: v })} />
          <TextField label="Survey invitation" value={cl.surveyInvitation} placeholder="Stay on the line for a one-question survey"
            onChange={(v) => patch("closing", { surveyInvitation: v })} />
          <TextField label="Unresolved closing" value={cl.unresolvedClosing} placeholder="What to say when the issue stays open"
            onChange={(v) => patch("closing", { unresolvedClosing: v })} />
          <TextField label="Transferred closing" value={cl.transferredClosing} placeholder="What to say right before a transfer"
            onChange={(v) => patch("closing", { transferredClosing: v })} />
        </div>
      </Sec>

      <Sec title="Special situations" hint="Responses for edge cases">
        <div className="grid grid-2" style={{ gap: 12 }}>
          {SPECIAL_FIELDS.map((f) => (
            <TextField key={f.key} label={f.label} value={sp[f.key]}
              onChange={(v) => patch("special", { [f.key]: v })} />
          ))}
        </div>
      </Sec>

      <Sec title="Advanced instructions" hint="Free-form additions appended to the compiled prompt">
        <TextField label="Instructions" rows={4} value={adv.instructions}
          placeholder="Anything the structured sections don’t cover…"
          onChange={(v) => patch("advanced", { instructions: v })} />
      </Sec>
    </>
  );
}

/* ---------- Compile preview (debounced) ---------- */

function CompilePreview({ cfg }: { cfg: StructuredPromptConfig }) {
  const [result, setResult] = useState<PromptCompileResult | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const seq = useRef(0);
  const cfgJson = JSON.stringify(cfg);

  useEffect(() => {
    const id = ++seq.current;
    setLoading(true);
    const t = setTimeout(() => {
      compilePromptPreview({ promptMode: "structured", structuredConfig: JSON.parse(cfgJson) as StructuredPromptConfig })
        .then((r) => {
          if (seq.current !== id) return;
          setResult(r);
          setErr(null);
          setLoading(false);
        })
        .catch((e: unknown) => {
          if (seq.current !== id) return;
          setErr(errMsg(e, "Preview failed"));
          setLoading(false);
        });
    }, 800);
    return () => clearTimeout(t);
  }, [cfgJson]);

  return (
    <Sec title="Preview compiled prompt" hint="Deterministic compile of the configuration above" defaultOpen>
      {err && <Callout tone="critical" title="Preview failed">{err}</Callout>}
      {result && result.errors.length > 0 && (
        <Callout tone="critical" title="Configuration issues">
          <ul style={{ margin: 0, paddingLeft: 16 }}>
            {result.errors.map((e, i) => (
              <li key={i}><code>{e.field}</code> — {e.message}</li>
            ))}
          </ul>
        </Callout>
      )}
      <div className="row gap-8 t-micro" style={{ alignItems: "center", minHeight: 18 }}>
        {loading && <span className="spinner" aria-hidden />}
        {result && (
          <span className="t-num">
            {result.characterCount.toLocaleString()} characters · ~{result.tokenEstimate.toLocaleString()} tokens
          </span>
        )}
        {result && !result.valid && <span className="chip chip-critical">invalid</span>}
      </div>
      <pre style={preStyle}>{result?.compiled || (loading ? "Compiling…" : "Nothing to preview yet.")}</pre>
    </Sec>
  );
}

/* ---------- Full / unified prompt editor ---------- */

function FullPromptEditor({ value, onChange }: {
  value: string; onChange: (v: string) => void;
}) {
  const variables = detectVariables(value);
  return (
    <Sec title="Prompt text" hint="The complete system prompt, exactly as the runtime uses it" defaultOpen>
      <textarea
        className="textarea"
        aria-label="Full prompt text"
        value={value}
        style={{
          minHeight: 420, fontSize: 12.5, lineHeight: 1.6,
          fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
        }}
        onChange={(e) => onChange(e.target.value)}
      />
      <div className="row-between wrap gap-8">
        <span className="chip chip-neutral t-num">
          {value.length.toLocaleString()} characters · ~{Math.ceil(value.length / 4).toLocaleString()} tokens
        </span>
        <span className="row gap-6 wrap" style={{ justifyContent: "flex-end" }}>
          <span className="t-micro">Variables</span>
          {variables.length === 0 && <span className="t-micro">none — add {"{placeholders}"} for caller details</span>}
          {variables.map((v) => <span key={v} className="chip chip-brand">{`{${v}}`}</span>)}
        </span>
      </div>
    </Sec>
  );
}

/* ---------- Full-prompt render preview (debounced, with test data) ---------- */

function FullPromptPreview({ fullPrompt }: { fullPrompt: string }) {
  const [testJson, setTestJson] = useState('{"customer_name": "Rahul Sharma"}');
  const [result, setResult] = useState<PromptCompileResult | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [jsonErr, setJsonErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const seq = useRef(0);

  useEffect(() => {
    const id = ++seq.current;
    let testContext: Record<string, unknown>;
    try {
      testContext = testJson.trim() ? (JSON.parse(testJson) as Record<string, unknown>) : {};
      setJsonErr(null);
    } catch {
      setJsonErr("Test data must be valid JSON.");
      return;
    }
    setLoading(true);
    const t = setTimeout(() => {
      compilePromptPreview({ promptMode: "full", fullPrompt, testContext })
        .then((r) => {
          if (seq.current !== id) return;
          setResult(r);
          setErr(null);
          setLoading(false);
        })
        .catch((e: unknown) => {
          if (seq.current !== id) return;
          setErr(errMsg(e, "Preview failed"));
          setLoading(false);
        });
    }, 800);
    return () => clearTimeout(t);
  }, [fullPrompt, testJson]);

  const render = result?.render;
  return (
    <Sec title="Preview with test data" hint="The prompt a live call would run, rendered with sample context values">
      <Field label="Test context (JSON)" error={jsonErr ?? undefined}
        hint="Sample runtime-context values — live calls resolve these from the bot's runtime context.">
        <textarea
          className="textarea"
          rows={3}
          value={testJson}
          style={{ fontSize: 12, fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace" }}
          onChange={(e) => setTestJson(e.target.value)}
        />
      </Field>
      {err && <Callout tone="critical" title="Preview failed">{err}</Callout>}
      {result && result.errors.length > 0 && (
        <Callout tone="critical" title="Prompt issues">
          <ul style={{ margin: 0, paddingLeft: 16 }}>
            {result.errors.map((e, i) => (
              <li key={i}><code>{e.field}</code> — {e.message}</li>
            ))}
          </ul>
        </Callout>
      )}
      {render && render.missing.length > 0 && (
        <Callout tone="warning" title={`No value for ${render.missing.map((m) => `{${m}}`).join(", ")}`}>
          These variables have no value in the test data — on a live call the bot is told they are unknown.
        </Callout>
      )}
      <div className="row gap-8 t-micro" style={{ alignItems: "center", minHeight: 18 }}>
        {loading && <span className="spinner" aria-hidden />}
        {result && (
          <span className="t-num">
            {result.characterCount.toLocaleString()} characters · ~{result.tokenEstimate.toLocaleString()} tokens
          </span>
        )}
        {result && !result.valid && <span className="chip chip-critical">invalid</span>}
        {render && render.unusedTestKeys.length > 0 && (
          <span>unused test keys: {render.unusedTestKeys.join(", ")}</span>
        )}
      </div>
      <pre style={preStyle}>{render?.rendered || (loading ? "Rendering…" : "Nothing to preview yet.")}</pre>
    </Sec>
  );
}

/* ---------- Simple (per-language variant) editor ---------- */

function SimpleEditor({ variants, variables, languages, onChange }: {
  variants: PromptVariant[]; variables: string[]; languages: Lang[];
  onChange: (next: PromptVariant[]) => void;
}) {
  const [lang, setLang] = useState(variants[0]?.language ?? "en-US");
  const current = variants.find((v) => v.language === lang) ?? variants[0];
  const available = languages.filter((l) => !variants.some((v) => v.language === l.code));

  const setContent = (content: string) =>
    onChange(variants.map((v) => (v.language === current?.language ? { ...v, content } : v)));

  const addLanguage = (code: string) => {
    if (!code || variants.some((v) => v.language === code)) return;
    onChange([...variants, { language: code, content: "" }]);
    setLang(code);
  };

  return (
    <div className="col gap-12">
      <div className="row gap-6 wrap">
        {variants.map((v) => (
          <button
            key={v.language}
            className={`chip ${v.language === current?.language ? "chip-brand" : "chip-neutral"}`}
            onClick={() => setLang(v.language)}
          >
            {v.language}
          </button>
        ))}
        {available.length > 0 && (
          <select
            className="select"
            value=""
            aria-label="Add language"
            style={{ width: "auto", minWidth: 160, fontSize: 12 }}
            onChange={(e) => addLanguage(e.target.value)}
          >
            <option value="">+ Add language…</option>
            {available.map((l) => (
              <option key={l.code} value={l.code}>{l.name} ({l.code})</option>
            ))}
          </select>
        )}
      </div>

      <Field
        label={`Prompt text (${current?.language ?? lang})`}
        hint={`Variables resolve at call time: ${variables.length ? variables.join(", ") : "none"}`}
      >
        <textarea
          className="textarea"
          style={{ minHeight: 140, fontSize: 13.5 }}
          value={current?.content ?? ""}
          onChange={(e) => setContent(e.target.value)}
        />
      </Field>

      <div className="card-pad-sm" style={{ background: "var(--surface-2)", borderRadius: 10 }}>
        <span className="t-label">Caller hears</span>
        <p className="t-body mt-8" style={{ fontStyle: "italic" }}>
          “{Object.entries(PREVIEW_VARS).reduce((s, [k, val]) => s.split(k).join(val), current?.content ?? "")}”
        </p>
      </div>
    </div>
  );
}

/* ---------- Version history ---------- */

function VersionHistory({ prompt, isSystem, perms, busy, dirty, onRollback, onRestore }: {
  prompt: Prompt; isSystem: boolean; perms: Perms; busy: boolean; dirty: boolean;
  onRollback: (version: number) => void; onRestore: (ver: PromptVersion) => void;
}) {
  const [viewVer, setViewVer] = useState<number | null>(null);
  const [compareSel, setCompareSel] = useState<number[]>([]);
  const latestVersion = prompt.versions[0]?.version;

  const versionText = (v: PromptVersion) =>
    isSystem
      ? (v.promptMode === "full"
          ? (v.fullPrompt ?? v.compiledPrompt ?? "No content")
          : (v.compiledPrompt ?? (v.structuredConfig ? JSON.stringify(v.structuredConfig, null, 2) : "No content")))
      : (v.variants.map((x) => `[${x.language}]\n${x.content}`).join("\n\n") || "No content");

  const toggleCompare = (n: number) =>
    setCompareSel((sel) => (sel.includes(n) ? sel.filter((x) => x !== n) : [...sel.slice(-1), n]));

  const cmp: PromptVersion[] = compareSel.length === 2
    ? [...compareSel].sort((a, b) => a - b)
        .map((n) => prompt.versions.find((v) => v.version === n))
        .filter((v): v is PromptVersion => !!v)
    : [];

  const viewed = viewVer != null ? prompt.versions.find((v) => v.version === viewVer) : undefined;
  const dirtyHint = dirty ? "Save or discard your changes first" : undefined;

  return (
    <Sec
      title="Version history"
      hint={`${prompt.versions.length} version${prompt.versions.length === 1 ? "" : "s"} — select two to compare`}
    >
      {cmp.length === 2 && (
        <div className="grid grid-2" style={{ gap: 8 }}>
          {cmp.map((v) => (
            <div key={v.version} className="col gap-4">
              <span className="t-micro t-strong">
                v{v.version}{v.version === prompt.activeVersion ? " (active)" : ""}
              </span>
              <pre style={preStyle}>{versionText(v)}</pre>
            </div>
          ))}
        </div>
      )}

      {viewed && (
        <div className="col gap-6 card-pad-sm" style={{ background: "var(--surface-2)", borderRadius: 10 }}>
          <div className="row-between gap-8">
            <span className="t-micro t-strong">
              v{viewed.version} · {viewed.editedBy} · {fmtDate(viewed.editedAt)}{viewed.note ? ` · ${viewed.note}` : ""}
            </span>
            <button className="btn-icon" onClick={() => setViewVer(null)} aria-label="Close version preview">
              <Icon name="x" size={13} />
            </button>
          </div>
          <pre style={preStyle}>{versionText(viewed)}</pre>
        </div>
      )}

      {prompt.versions.map((v) => (
        <div
          key={v.version}
          className="row gap-12 card-pad-sm wrap"
          style={{ border: "1px solid var(--hairline)", borderRadius: 10, alignItems: "center" }}
        >
          <span
            className={`icon-tile ${v.version === prompt.activeVersion ? "good" : "neutral"}`}
            style={{ width: 28, height: 28, flexShrink: 0 }}
          >
            <Icon name="version" size={13} />
          </span>
          <div className="grow" style={{ minWidth: 140 }}>
            <span className="t-strong" style={{ fontSize: 12.5 }}>
              v{v.version}
              {v.version === prompt.activeVersion && <span className="chip chip-good" style={{ marginLeft: 6 }}>active</span>}
              {prompt.publishedVersion != null && v.version === prompt.publishedVersion && (
                <span className="chip chip-info" style={{ marginLeft: 6 }}>published</span>
              )}
            </span>
            <div className="t-micro">
              {v.editedBy} · {fmtDate(v.editedAt)}{v.note ? ` · ${v.note}` : ""}
            </div>
          </div>
          <span className="row gap-4 wrap">
            <Button size="sm" variant="ghost" icon="eye"
              onClick={() => setViewVer(viewVer === v.version ? null : v.version)}>
              View
            </Button>
            <button
              className={`chip ${compareSel.includes(v.version) ? "chip-brand" : "chip-neutral"}`}
              onClick={() => toggleCompare(v.version)}
            >
              Compare
            </button>
            {perms.canManage && v.version !== latestVersion && (
              <Button size="sm" variant="ghost" icon="copy" disabled={busy || dirty}
                title={dirtyHint ?? "Restore as a new draft version"}
                onClick={() => onRestore(v)}>
                Restore
              </Button>
            )}
            {perms.canPublish && v.version !== prompt.activeVersion && (
              <Button size="sm" variant="ghost" icon="undo" disabled={busy || dirty}
                title={dirtyHint ?? "Move the active pointer to this version"}
                onClick={() => onRollback(v.version)}>
                {v.version < prompt.activeVersion ? "Roll back" : "Set active"}
              </Button>
            )}
          </span>
        </div>
      ))}
    </Sec>
  );
}

/* ---------- Test drawer ---------- */

function TestDrawer({ prompt, languages, onClose }: {
  prompt: Prompt; languages: Lang[]; onClose: () => void;
}) {
  const [message, setMessage] = useState("");
  const [language, setLanguage] = useState(
    () => languages.find((l) => l.code === "en-US")?.code ?? languages[0]?.code ?? "en-US",
  );
  const [version, setVersion] = useState(prompt.activeVersion);
  const [useKnowledge, setUseKnowledge] = useState(true);
  const [running, setRunning] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [result, setResult] = useState<PromptTestResult | null>(null);

  const langOptions: Lang[] = languages.some((l) => l.code === language)
    ? languages
    : [{ code: language, name: language }, ...languages];

  const run = async () => {
    const trimmed = message.trim();
    if (!trimmed || running) return;
    setRunning(true);
    setErr(null);
    try {
      setResult(await runPromptTest(prompt.id, { message: trimmed, language, version, useKnowledge }));
    } catch (e) {
      setErr(errMsg(e, "Test failed"));
    } finally {
      setRunning(false);
    }
  };

  const confidence = result
    ? Math.round(result.intentConfidence <= 1 ? result.intentConfidence * 100 : result.intentConfidence)
    : 0;

  return (
    <Drawer
      open
      onClose={onClose}
      title={<span className="row gap-8">Test “{prompt.name}”<StatusChip status={prompt.state} /></span>}
      sub={`${typeLabel(prompt.type)} · runs against the selected version without touching live calls`}
    >
      <div className="col gap-12">
        <Field label="Test message" required>
          <textarea
            className="textarea" rows={3} value={message}
            placeholder="What would the caller say?"
            onChange={(e) => setMessage(e.target.value)}
          />
        </Field>
        <div className="grid grid-2" style={{ gap: 12 }}>
          <Field label="Language">
            <select className="select" value={language} onChange={(e) => setLanguage(e.target.value)}>
              {langOptions.map((l) => (
                <option key={l.code} value={l.code}>{l.name} ({l.code})</option>
              ))}
            </select>
          </Field>
          <Field label="Version">
            <select className="select" value={version} onChange={(e) => setVersion(Number(e.target.value))}>
              {(prompt.versions.length ? prompt.versions : [{ version: prompt.activeVersion }]).map((v) => (
                <option key={v.version} value={v.version}>
                  v{v.version}{v.version === prompt.activeVersion ? " (active)" : ""}
                </option>
              ))}
            </select>
          </Field>
        </div>
        <ToggleRow
          label="Use knowledge base"
          hint="Ground the answer in indexed sources"
          checked={useKnowledge}
          onChange={setUseKnowledge}
        />
        <Button
          variant="primary" icon="play" busy={running} disabled={!message.trim()}
          style={{ alignSelf: "flex-start" }}
          onClick={() => void run()}
        >
          Run test
        </Button>

        {err && <Callout tone="critical" title="Test failed">{err}</Callout>}

        {result && (
          <div className="col gap-12">
            {result.error && <Callout tone="critical" title="Runtime error">{result.error}</Callout>}
            <div className="card-pad-sm" style={{ background: "var(--surface-2)", borderRadius: 10 }}>
              <span className="t-label">Response</span>
              <p className="t-body mt-8" style={{ whiteSpace: "pre-wrap", margin: "8px 0 0" }}>
                {result.response || "—"}
              </p>
            </div>
            <div className="row gap-6 wrap">
              <span className="chip chip-info">route: {result.route}</span>
              {result.matchedIntent && (
                <span className="chip chip-brand">{result.matchedIntent} · {confidence}%</span>
              )}
              <StatusChip
                status={result.usedKnowledgeBase ? "good" : "neutral"}
                label={result.usedKnowledgeBase ? "knowledge used" : "no knowledge"}
              />
              {result.provider && <span className="chip chip-neutral">{result.provider}</span>}
            </div>
            <span className="t-micro t-num">
              v{result.promptVersion} · {result.language} · {result.latencyMs}ms ·{" "}
              {result.tokens.input} in / {result.tokens.output} out tokens
            </span>
            {result.sources.length > 0 && (
              <div className="col gap-6">
                <span className="t-label">Knowledge sources</span>
                {result.sources.map((s, i) => (
                  <div key={i} className="col gap-4 card-pad-sm" style={{ border: "1px solid var(--hairline)", borderRadius: 10 }}>
                    <div className="row-between gap-8">
                      <span className="t-strong truncate" style={{ fontSize: 12.5 }}>{s.documentName}</span>
                      <span className="t-micro t-num" style={{ whiteSpace: "nowrap" }}>score {s.score.toFixed(2)}</span>
                    </div>
                    {s.text && <p className="t-sub" style={{ fontSize: 12, margin: 0 }}>{s.text}</p>}
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    </Drawer>
  );
}

/* ---------- Small form building blocks ---------- */

function Sec({ title, hint, defaultOpen, children }: {
  title: string; hint?: string; defaultOpen?: boolean; children: ReactNode;
}) {
  return (
    <details className="card" open={defaultOpen}>
      <summary className="row-between card-pad-sm" style={{ cursor: "pointer", listStyle: "none", gap: 12 }}>
        <span className="col" style={{ gap: 1 }}>
          <span className="t-strong" style={{ fontSize: 13.5 }}>{title}</span>
          {hint && <span className="t-micro">{hint}</span>}
        </span>
        <Icon name="chevron-down" size={14} style={{ color: "var(--ink-3)", flexShrink: 0 }} />
      </summary>
      <div className="col gap-12" style={{ padding: "4px 16px 16px", borderTop: "1px solid var(--hairline)" }}>
        {children}
      </div>
    </details>
  );
}

function TextField({ label, value, onChange, hint, placeholder, required, rows }: {
  label: string; value?: string; onChange: (v: string | undefined) => void;
  hint?: string; placeholder?: string; required?: boolean; rows?: number;
}) {
  return (
    <Field label={label} hint={hint} required={required}>
      {rows ? (
        <textarea
          className="textarea" rows={rows} value={value ?? ""} placeholder={placeholder}
          onChange={(e) => onChange(e.target.value || undefined)}
        />
      ) : (
        <input
          className="input" value={value ?? ""} placeholder={placeholder}
          onChange={(e) => onChange(e.target.value || undefined)}
        />
      )}
    </Field>
  );
}

function SelectField({ label, value, onChange, options, hint }: {
  label: string; value?: string; onChange: (v: string | undefined) => void;
  options: readonly string[]; hint?: string;
}) {
  return (
    <Field label={label} hint={hint}>
      <select className="select" value={value ?? ""} onChange={(e) => onChange(e.target.value || undefined)}>
        <option value="">Default</option>
        {options.map((o) => (
          <option key={o} value={o}>{o.charAt(0).toUpperCase() + o.slice(1)}</option>
        ))}
      </select>
    </Field>
  );
}

function NumField({ label, value, onChange, min, max, hint }: {
  label: string; value?: number; onChange: (v: number | undefined) => void;
  min?: number; max?: number; hint?: string;
}) {
  return (
    <Field label={label} hint={hint}>
      <input
        className="input" type="number" min={min} max={max} value={value ?? ""}
        onChange={(e) => {
          if (e.target.value === "") { onChange(undefined); return; }
          const n = Number(e.target.value);
          if (Number.isNaN(n)) return;
          onChange(Math.min(max ?? Infinity, Math.max(min ?? -Infinity, n)));
        }}
      />
    </Field>
  );
}

function ToggleRow({ label, hint, checked, onChange }: {
  label: string; hint?: string; checked: boolean; onChange: (v: boolean) => void;
}) {
  return (
    <div className="row-between" style={{ padding: "4px 0", gap: 12 }}>
      <span className="col" style={{ gap: 1 }}>
        <span style={{ fontSize: 13, fontWeight: 550 }}>{label}</span>
        {hint && <span className="t-micro">{hint}</span>}
      </span>
      <Toggle checked={checked} onChange={onChange} label={label} />
    </div>
  );
}
