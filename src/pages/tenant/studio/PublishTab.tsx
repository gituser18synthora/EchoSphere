import { useState } from "react";
import { useNavigate } from "react-router-dom";
import type { Release, VoiceBot } from "@/types/domain";
import { useAsync } from "@/hooks/useAsync";
import { createRelease, listReleases, updateReleaseStage } from "@/services/api";
import { Button, Callout, CardSkeleton, ConfirmModal, Drawer, ErrorState, Field, Modal, StatusChip, Timeline } from "@/components/ui";
import { Icon } from "@/components/Icon";
import { useApp } from "@/state/AppContext";
import { flags } from "@/services/flags";
import { openRelease, suggestedVersion } from "@/services/releaseVersion";

const stages = ["draft", "review", "approved", "published"] as const;

function CreateReleaseCard({ bot, releases, onCreated }: { bot: VoiceBot; releases: Release[]; onCreated: () => void }) {
  const { toast } = useApp();
  const navigate = useNavigate();
  const [version, setVersion] = useState(() => suggestedVersion(bot, releases));
  const [notes, setNotes] = useState("");
  const [busy, setBusy] = useState(false);
  const pending = bot.readiness.filter((r) => !r.done);
  const versionError = version.trim() ? undefined : "Version is required";

  const submit = async () => {
    if (versionError) return;
    setBusy(true);
    try {
      await createRelease(bot.id, { version: version.trim(), notes: notes.trim() || undefined });
      toast(`Release ${version.trim()} created — review the checklist, then approve and publish`);
      onCreated();
    } catch (e) {
      toast(e instanceof Error ? e.message : "Could not create release", "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="card">
      <div className="card-header">
        <div className="col gap-2">
          <span className="card-title">{releases.length === 0 ? "Create the first release" : "Create the next release"}</span>
          <span className="t-micro">Nothing reaches callers without an approved, versioned release.</span>
        </div>
        <span className="t-micro t-num">{bot.readiness.length - pending.length}/{bot.readiness.length} readiness checks done</span>
      </div>
      <div className="col gap-12" style={{ padding: 16 }}>
        {pending.length > 0 && (
          <Callout tone="warning" title={`${pending.length} readiness ${pending.length === 1 ? "item is" : "items are"} still open`}>
            <div className="col gap-4 mt-4">
              {pending.map((r) => (
                <button key={r.id} type="button" className="row gap-6"
                  style={{ fontSize: 13, textAlign: "left", background: "none", border: 0, padding: 0, cursor: "pointer", color: "var(--brand-600)", fontWeight: 550 }}
                  onClick={() => navigate(`/t/bots/${bot.id}/${r.studioTab}`)}>
                  <Icon name="alert" size={13} /> {r.label}
                </button>
              ))}
            </div>
            <div className="t-micro mt-8">You can create the release now; publishing stays blocked until the checklist passes.</div>
          </Callout>
        )}
        <div className="grid grid-2 gap-12">
          <Field label="Version" required error={versionError} hint="Semantic version, e.g. v0.1.0">
            <input className="input" value={version} onChange={(e) => setVersion(e.target.value)} maxLength={20} placeholder="v0.1.0" />
          </Field>
          <Field label="Release notes" hint="What changed — shown in the release history and audit log">
            <textarea className="textarea" rows={2} value={notes} onChange={(e) => setNotes(e.target.value)} maxLength={2000}
              placeholder="e.g. Initial go-live: FAQ journey, Hindi + English voices" />
          </Field>
        </div>
        <div className="row" style={{ justifyContent: "flex-end" }}>
          <Button variant="primary" icon="rocket" busy={busy} disabled={Boolean(versionError)} onClick={submit}>Create release</Button>
        </div>
      </div>
    </div>
  );
}

export default function PublishTab({ bot }: { bot: VoiceBot }) {
  const q = useAsync(() => listReleases(bot.id), [bot.id]);
  const { toast, user } = useApp();
  const isSuperAdmin = user?.role === "super_admin";
  const [approvalOpen, setApprovalOpen] = useState(false);
  const [approvalNote, setApprovalNote] = useState("");
  const [overrideOpen, setOverrideOpen] = useState(false);
  const [overrideReason, setOverrideReason] = useState("");
  const [rollbackOpen, setRollbackOpen] = useState(false);
  const [busy, setBusy] = useState(false);

  if (q.error) return <ErrorState message={q.error} onRetry={q.reload} />;
  if (q.loading) return <CardSkeleton rows={8} />;

  const releases = q.data ?? [];
  const current = openRelease(releases);
  const published = releases.find((r) => r.stage === "published");

  const checklistFailing = Boolean(current && current.checklist.some((c) => !c.ok));

  const transition = async (releaseId: string, stage: string, msg: string, extra?: { note?: string; overrideReason?: string }) => {
    setBusy(true);
    try {
      await updateReleaseStage(releaseId, stage, extra);
      toast(msg);
      q.reload();
      return true;
    } catch (e) {
      toast(e instanceof Error ? e.message : "Release update failed", "error");
      return false;
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="col gap-16">
      {/* No release in flight → let the team start one (first or next) */}
      {!current && (
        <CreateReleaseCard key={releases.length} bot={bot} releases={releases} onCreated={q.reload} />
      )}

      {/* Pipeline banner */}
      {current && (
        <div className="card card-pad">
          <div className="row-between wrap gap-12">
            <div>
              <div className="row gap-8">
                <span className="t-strong" style={{ fontSize: 15 }}>Release {current.version}</span>
                <StatusChip status={current.stage} />
              </div>
              <p className="t-sub mt-4">{current.notes}</p>
              <p className="t-micro mt-4">Requested by {current.requestedBy}{current.approvedBy ? ` · approved by ${current.approvedBy}` : ""}</p>
            </div>
            <div className="row gap-6">
              {current.stage === "review" && (
                <Button variant="primary" icon="eye" onClick={() => setApprovalOpen(true)}>Review & approve</Button>
              )}
              {current.stage === "approved" && (
                <>
                  {flags.scheduledPublish && <Button icon="calendar">Schedule</Button>}
                  {checklistFailing && isSuperAdmin ? (
                    <Button variant="primary" icon="rocket" busy={busy} onClick={() => setOverrideOpen(true)}>
                      Publish with override
                    </Button>
                  ) : (
                    <Button variant="primary" icon="rocket" busy={busy} disabled={checklistFailing}
                      title={checklistFailing ? "Checklist items are failing — fix them or ask a super admin to override" : undefined}
                      onClick={() => transition(current.id, "published", `${current.version} publishing — traffic shifts over the next 60s`)}>
                      Publish now
                    </Button>
                  )}
                </>
              )}
            </div>
          </div>

          {/* Stage rail */}
          <div className="row mt-16" style={{ gap: 0 }}>
            {stages.map((s, i) => {
              const activeIdx = stages.indexOf(current.stage as (typeof stages)[number]);
              const state = i < activeIdx ? "done" : i === activeIdx ? "active" : "todo";
              return (
                <div key={s} className="row grow" style={{ gap: 0 }}>
                  <div className="col" style={{ alignItems: "center", gap: 5, flex: "0 0 auto" }}>
                    <span
                      style={{
                        width: 26, height: 26, borderRadius: "50%", display: "flex", alignItems: "center", justifyContent: "center",
                        background: state === "done" ? "var(--status-good-bg)" : state === "active" ? "var(--brand-500)" : "var(--surface-3)",
                        color: state === "done" ? "var(--status-good)" : state === "active" ? "#fff" : "var(--ink-3)",
                        fontSize: 11, fontWeight: 700,
                      }}
                    >
                      {state === "done" ? <Icon name="check" size={12} /> : i + 1}
                    </span>
                    <span className="t-micro" style={{ textTransform: "capitalize", fontWeight: state === "active" ? 700 : 500, color: state === "active" ? "var(--ink)" : undefined }}>{s}</span>
                  </div>
                  {i < stages.length - 1 && (
                    <div style={{ height: 2, background: i < activeIdx ? "var(--status-good)" : "var(--hairline)", flex: 1, margin: "13px 8px 0" }} />
                  )}
                </div>
              );
            })}
          </div>
        </div>
      )}

      <div className="grid grid-2">
        {/* Checklist */}
        {current && (
          <div className="card">
            <div className="card-header">
              <span className="card-title">Release checklist</span>
              <span className="t-micro t-num">{current.checklist.filter((c) => c.ok).length}/{current.checklist.length} passing</span>
            </div>
            <div className="col" style={{ padding: 16, gap: 8 }}>
              {current.checklist.map((c) => (
                <div key={c.id} className="row gap-10 card-pad-sm" style={{ borderRadius: 10, background: c.ok ? "var(--surface-2)" : "var(--status-warning-bg)" }}>
                  <Icon name={c.ok ? "check-circle" : "alert"} size={15}
                    style={{ color: c.ok ? "var(--status-good)" : "var(--status-warning)", flexShrink: 0 }} />
                  <div className="grow">
                    <span style={{ fontSize: 13, fontWeight: 550 }}>{c.label}</span>
                    {c.detail && <div className="t-micro">{c.detail}</div>}
                  </div>
                </div>
              ))}
              {current.checklist.some((c) => !c.ok) && (
                <Callout tone="warning">
                  Publishing is blocked while checklist items fail. A super admin can override with a justification, which is recorded in the audit log.
                </Callout>
              )}
            </div>
          </div>
        )}

        {/* Diff */}
        {current && (
          <div className="card">
            <div className="card-header">
              <span className="card-title">What changes in {current.version}</span>
              <span className="t-micro">vs live {published?.version ?? "—"}</span>
            </div>
            <div className="col" style={{ padding: 16, gap: 8 }}>
              {current.diff.map((d, i) => (
                <div key={i} className="row gap-10 card-pad-sm" style={{ border: "1px solid var(--hairline)", borderRadius: 10 }}>
                  <span className={`chip chip-${d.kind === "added" ? "good" : d.kind === "removed" ? "critical" : "info"}`} style={{ minWidth: 68, justifyContent: "center" }}>
                    {d.kind}
                  </span>
                  <div>
                    <span className="t-micro t-strong">{d.area}</span>
                    <div style={{ fontSize: 12.5 }}>{d.change}</div>
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>

      {/* Live version + rollback */}
      {published && (
        <div className="card card-pad row-between wrap gap-12">
          <div className="row gap-12">
            <span className="icon-tile good"><Icon name="rocket" size={16} /></span>
            <div>
              <span className="t-strong">Live: {published.version}</span>
              <div className="t-micro">Published {published.publishedAt ? new Date(published.publishedAt).toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : ""} · approved by {published.approvedBy}</div>
            </div>
          </div>
          <Button variant="danger-ghost" icon="undo" onClick={() => setRollbackOpen(true)}>Roll back</Button>
        </div>
      )}

      {/* History */}
      {releases.length > 0 && (
      <div className="card">
        <div className="card-header"><span className="card-title">Release history</span></div>
        <div style={{ padding: "16px 20px" }}>
          <Timeline
            items={releases.map((r) => ({
              icon: r.stage === "published" ? "rocket" : r.stage === "rolled_back" ? "undo" : r.stage === "review" ? "eye" : "clock",
              tone: r.stage === "published" ? "good" : r.stage === "rolled_back" ? "critical" : "brand",
              title: <span className="row gap-8">{r.version}<StatusChip status={r.stage} /></span>,
              meta: `${r.requestedBy}${r.approvedBy ? ` · approved by ${r.approvedBy}` : ""}${r.publishedAt ? ` · ${new Date(r.publishedAt).toLocaleDateString("en-US", { month: "short", day: "numeric" })}` : ""}`,
              body: r.notes,
            }))}
          />
        </div>
      </div>
      )}

      {/* Approval drawer */}
      {current && (
        <Drawer
          open={approvalOpen}
          onClose={() => setApprovalOpen(false)}
          wide
          title={`Approve release ${current.version}?`}
          sub="Approval allows publishing but does not publish. Your decision and note are recorded in the audit log."
          footer={
            <>
              <Button variant="secondary" icon="x" busy={busy} onClick={async () => { if (await transition(current.id, "draft", `${current.version} sent back to draft with your notes`, { note: approvalNote.trim() || undefined })) setApprovalOpen(false); }}>
                Request changes
              </Button>
              <Button variant="primary" icon="check" busy={busy} disabled={!approvalNote.trim()} onClick={async () => { if (await transition(current.id, "approved", `${current.version} approved — ready to publish`, { note: approvalNote.trim() })) setApprovalOpen(false); }}>
                Approve release
              </Button>
            </>
          }
        >
          <div className="col gap-16">
            {current.checklist.some((c) => !c.ok) && (
              <Callout tone="critical" title={`${current.checklist.filter((c) => !c.ok).length} checklist items failing`}>
                You can approve now, but publishing stays blocked until these pass (or a super admin overrides with a justification).
              </Callout>
            )}
            <div>
              <span className="t-label">Changes</span>
              <div className="col gap-6 mt-8">
                {current.diff.map((d, i) => (
                  <div key={i} className="row gap-8" style={{ fontSize: 13 }}>
                    <span className={`chip chip-${d.kind === "added" ? "good" : d.kind === "removed" ? "critical" : "info"}`}>{d.kind}</span>
                    <span><b>{d.area}:</b> {d.change}</span>
                  </div>
                ))}
              </div>
            </div>
            <label className="field">
              <span className="field-label">Approval note (required)</span>
              <textarea className="textarea" value={approvalNote} onChange={(e) => setApprovalNote(e.target.value)} maxLength={2000}
                placeholder="e.g. Verified wait-time variable in staging; stale-source re-sync scheduled before publish window." />
            </label>
          </div>
        </Drawer>
      )}

      {current && (
        <Modal
          open={overrideOpen}
          onClose={() => setOverrideOpen(false)}
          title={`Publish ${current.version} with override?`}
          sub="Super-admin only. The justification and the failing checklist items are written to the audit log."
          footer={
            <>
              <Button variant="secondary" onClick={() => setOverrideOpen(false)}>Cancel</Button>
              <Button variant="primary" icon="rocket" busy={busy} disabled={overrideReason.trim().length < 10}
                onClick={async () => {
                  if (await transition(current.id, "published", `${current.version} published with override — traffic shifts over the next 60s`, { overrideReason: overrideReason.trim() })) {
                    setOverrideOpen(false);
                    setOverrideReason("");
                  }
                }}>
                Publish anyway
              </Button>
            </>
          }
        >
          <div className="col gap-12">
            <Callout tone="critical" title="Failing checks">
              <ul style={{ margin: "4px 0 0 16px", fontSize: 13 }}>
                {current.checklist.filter((c) => !c.ok).map((c) => <li key={c.id}>{c.label}{c.detail ? ` — ${c.detail}` : ""}</li>)}
              </ul>
            </Callout>
            <Field label="Justification" required hint="At least 10 characters">
              <textarea className="textarea" rows={3} value={overrideReason} onChange={(e) => setOverrideReason(e.target.value)} maxLength={2000}
                placeholder="e.g. Imported tenant — regression suite is being authored; go-live approved by the customer for a supervised pilot." />
            </Field>
          </div>
        </Modal>
      )}

      <ConfirmModal
        open={rollbackOpen}
        onClose={() => setRollbackOpen(false)}
        danger
        busy={busy}
        title={`Roll back ${bot.name}?`}
        confirmLabel="Roll back now"
        body={
          <>
            Live traffic switches from <code>{published?.version}</code> to the previous published version within 60 seconds.
            In-progress calls finish on the current version. A rollback release entry is created, the release that caused
            it is marked <b>rolled back</b>, and the team is notified.
          </>
        }
        onConfirm={async () => {
          if (published && await transition(published.id, "rolled_back", "Rollback initiated — traffic shifting to previous version")) {
            setRollbackOpen(false);
          }
        }}
      />
    </div>
  );
}
