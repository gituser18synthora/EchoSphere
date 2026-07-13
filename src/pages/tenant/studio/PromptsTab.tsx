import { useMemo, useState } from "react";
import type { Prompt, VoiceBot } from "@/types/domain";
import { useAsync } from "@/hooks/useAsync";
import { listPrompts, simulateAction } from "@/services/api";
import { Button, Callout, CardSkeleton, Drawer, EmptyState, StatusChip } from "@/components/ui";
import { Icon } from "@/components/Icon";
import { useApp } from "@/state/AppContext";

export default function PromptsTab({ bot }: { bot: VoiceBot }) {
  const q = useAsync(() => listPrompts(bot.id), [bot.id]);
  const { toast } = useApp();
  const [openPrompt, setOpenPrompt] = useState<Prompt | null>(null);

  return (
    <div className="col gap-16">
      <Callout tone="info" title="Business prompts only">
        You edit greeting, fallback, escalation and closing messages here. Platform system prompts, safety
        preambles and guardrails are managed centrally and never shown or editable at tenant level.
      </Callout>

      {q.loading && <div className="grid grid-2">{Array.from({ length: 4 }).map((_, i) => <CardSkeleton key={i} rows={3} />)}</div>}
      {q.error && <EmptyState icon="alert" title="Couldn’t load prompts" body={q.error} action={<Button icon="refresh" onClick={q.reload}>Retry</Button>} />}

      {q.data && q.data.length === 0 && (
        <EmptyState icon="message" title="No prompts yet" body="Create the greeting first — it's the first thing every caller hears." action={<Button variant="primary" icon="plus">New prompt</Button>} />
      )}

      <div className="grid grid-2">
        {(q.data ?? []).map((p) => {
          const active = p.versions.find((v) => v.version === p.activeVersion) ?? p.versions[0];
          const latest = p.versions[0];
          const pendingNewer = latest.version > p.activeVersion;
          return (
            <button key={p.id} className="card card-pad card-clickable col gap-10" style={{ textAlign: "left" }} onClick={() => setOpenPrompt(p)}>
              <div className="row-between">
                <span className="row gap-8">
                  <span className="tag" style={{ textTransform: "capitalize" }}>{p.type}</span>
                  <span className="t-strong" style={{ fontSize: 13.5 }}>{p.name}</span>
                </span>
                <StatusChip status={p.state} />
              </div>
              <p className="t-sub" style={{ fontSize: 12.5, lineHeight: 1.55, display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical", overflow: "hidden" }}>
                {active.variants[0].content}
              </p>
              <div className="row-between t-micro">
                <span className="row gap-8">
                  <span className="row gap-4"><Icon name="version" size={12} />v{p.activeVersion} active{pendingNewer ? ` · v${latest.version} pending` : ""}</span>
                  <span className="row gap-4"><Icon name="globe" size={12} />{active.variants.length} language{active.variants.length > 1 ? "s" : ""}</span>
                </span>
                {p.variables.length > 0 && <span className="row gap-4">{p.variables.map((v) => <code key={v} style={{ fontSize: 10.5, background: "var(--surface-3)", padding: "1px 5px", borderRadius: 4 }}>{v}</code>)}</span>}
              </div>
            </button>
          );
        })}
      </div>

      <PromptDrawer prompt={openPrompt} onClose={() => setOpenPrompt(null)} onAction={(m) => { toast(m); setOpenPrompt(null); q.reload(); }} />
    </div>
  );
}

function PromptDrawer({ prompt, onClose, onAction }: {
  prompt: Prompt | null; onClose: () => void; onAction: (msg: string) => void;
}) {
  const [lang, setLang] = useState("en-US");
  const [compareOpen, setCompareOpen] = useState(false);
  const [text, setText] = useState<string | null>(null);

  const latest = prompt?.versions[0];
  const active = prompt?.versions.find((v) => v.version === prompt.activeVersion) ?? latest;
  const variant = useMemo(
    () => latest?.variants.find((v) => v.language === lang) ?? latest?.variants[0],
    [latest, lang],
  );
  const activeVariant = active?.variants.find((v) => v.language === (variant?.language ?? lang)) ?? active?.variants[0];
  const value = text ?? variant?.content ?? "";
  const dirty = text !== null && text !== variant?.content;

  if (!prompt || !latest) return null;

  return (
    <Drawer
      open={!!prompt}
      onClose={onClose}
      wide
      title={<span className="row gap-8">{prompt.name}<StatusChip status={prompt.state} /></span>}
      sub={`Type: ${prompt.type} · active v${prompt.activeVersion} · latest v${latest.version} by ${latest.editedBy}`}
      footer={
        <>
          {prompt.state === "pending_approval" && (
            <>
              <Button variant="secondary" icon="undo" onClick={() => onAction(`v${latest.version} rejected — author notified with your note`)}>Request changes</Button>
              <Button variant="primary" icon="check" onClick={() => onAction(`v${latest.version} approved — will go live with the next publish`)}>Approve v{latest.version}</Button>
            </>
          )}
          {prompt.state !== "pending_approval" && (
            <Button variant="primary" icon="send" disabled={!dirty} title={dirty ? undefined : "Edit the text to submit a new version"}
              onClick={() => onAction(`Saved as v${latest.version + 1} and submitted for approval`)}>
              Submit for approval
            </Button>
          )}
        </>
      }
    >
      <div className="col gap-16">
        {/* Language variants */}
        <div className="row gap-6">
          {latest.variants.map((v) => (
            <button key={v.language} className={`chip ${v.language === (variant?.language ?? lang) ? "chip-brand" : "chip-neutral"}`}
              onClick={() => { setLang(v.language); setText(null); }}>
              {v.language}
            </button>
          ))}
          <button className="chip chip-neutral" onClick={() => onAction("Language variant scaffold created — translate and submit for approval")}>
            <Icon name="plus" size={11} /> Add language
          </button>
        </div>

        <div className="col gap-6">
          <span className="field-label">Prompt text ({variant?.language})</span>
          <textarea
            className="textarea"
            style={{ minHeight: 120, fontSize: 13.5 }}
            value={value}
            onChange={(e) => setText(e.target.value)}
            aria-label="Prompt text"
          />
          <span className="field-hint">
            Variables: {prompt.variables.length ? prompt.variables.map((v) => <code key={v} style={{ marginRight: 6 }}>{v}</code>) : "none"} — resolved at call time.
          </span>
        </div>

        {/* Preview */}
        <div className="card-pad-sm" style={{ background: "var(--surface-2)", borderRadius: 10 }}>
          <span className="t-label">Caller hears</span>
          <p className="t-body mt-8" style={{ fontStyle: "italic" }}>
            “{value.replace("{caller_name}", "Maria").replace("{clinic_name}", "Meridian Oakwood").replace("{queue_wait}", "two minutes").replace("{appointment_date}", "Thursday, July 9 at 10:15 AM")}”
          </p>
        </div>

        {/* Version history + compare */}
        <div className="col gap-8">
          <div className="row-between">
            <span className="t-label">Version history</span>
            {prompt.versions.length > 1 && (
              <button className="t-micro row gap-4" style={{ color: "var(--brand-600)", fontWeight: 600 }} onClick={() => setCompareOpen((o) => !o)}>
                <Icon name="history" size={12} /> {compareOpen ? "Hide diff" : `Compare v${active!.version} → v${latest.version}`}
              </button>
            )}
          </div>

          {compareOpen && activeVariant && variant && (
            <div className="grid grid-2" style={{ gap: 8 }}>
              <div className="card-pad-sm" style={{ background: "var(--status-critical-bg)", borderRadius: 10, fontSize: 12.5 }}>
                <span className="t-micro t-strong">v{active!.version} (active)</span>
                <p className="mt-4">{activeVariant.content}</p>
              </div>
              <div className="card-pad-sm" style={{ background: "var(--status-good-bg)", borderRadius: 10, fontSize: 12.5 }}>
                <span className="t-micro t-strong">v{latest.version} (latest)</span>
                <p className="mt-4">{variant.content}</p>
              </div>
            </div>
          )}

          {prompt.versions.map((v) => (
            <div key={v.version} className="row gap-12 card-pad-sm" style={{ border: "1px solid var(--hairline)", borderRadius: 10 }}>
              <span className={`icon-tile ${v.version === prompt.activeVersion ? "good" : "neutral"}`} style={{ width: 28, height: 28 }}>
                <Icon name="version" size={13} />
              </span>
              <div className="grow">
                <span className="t-strong" style={{ fontSize: 12.5 }}>
                  v{v.version} {v.version === prompt.activeVersion && <span className="chip chip-good" style={{ marginLeft: 6 }}>active</span>}
                </span>
                <div className="t-micro">{v.editedBy} · {new Date(v.editedAt).toLocaleDateString("en-US", { month: "short", day: "numeric" })} · {v.note}</div>
              </div>
              {v.version !== prompt.activeVersion && v.version < prompt.activeVersion && (
                <Button size="sm" variant="ghost" icon="undo" onClick={() => simulateAction("restore").then(() => onAction(`Restored v${v.version} as a new draft (v${latest.version + 1}) — approval required`))}>
                  Restore
                </Button>
              )}
            </div>
          ))}
        </div>
      </div>
    </Drawer>
  );
}
