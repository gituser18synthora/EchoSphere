import { useState } from "react";
import type { VoiceBot } from "@/types/domain";
import { useAsync } from "@/hooks/useAsync";
import { listReleases, updateReleaseStage } from "@/services/api";
import { Button, Callout, CardSkeleton, ConfirmModal, Drawer, ErrorState, StatusChip, Timeline } from "@/components/ui";
import { Icon } from "@/components/Icon";
import { useApp } from "@/state/AppContext";
import { flags } from "@/services/flags";

const stages = ["draft", "review", "approved", "published"] as const;

export default function PublishTab({ bot }: { bot: VoiceBot }) {
  const q = useAsync(() => listReleases(bot.id), [bot.id]);
  const { toast } = useApp();
  const [approvalOpen, setApprovalOpen] = useState(false);
  const [rollbackOpen, setRollbackOpen] = useState(false);
  const [busy, setBusy] = useState(false);

  if (q.error) return <ErrorState message={q.error} onRetry={q.reload} />;
  if (q.loading) return <CardSkeleton rows={8} />;

  const releases = q.data ?? [];
  const current = releases.find((r) => r.stage === "review" || r.stage === "draft" || r.stage === "approved");
  const published = releases.find((r) => r.stage === "published");

  if (releases.length === 0) {
    return (
      <Callout tone="info" title="No releases yet">
        Finish the readiness checklist on the Overview tab, then create the first release. Nothing reaches callers without an approved, versioned release.
      </Callout>
    );
  }

  const transition = async (releaseId: string, stage: string, msg: string) => {
    setBusy(true);
    try {
      await updateReleaseStage(releaseId, stage);
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
                  <Button variant="primary" icon="rocket" busy={busy}
                    onClick={() => transition(current.id, "published", `${current.version} publishing — traffic shifts over the next 60s`)}>
                    Publish now
                  </Button>
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
                  Publishing is blocked while checklist items fail. An approver can override with a justification, which is recorded in the audit log.
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
              <Button variant="secondary" icon="x" busy={busy} onClick={async () => { if (await transition(current.id, "draft", `${current.version} sent back to draft with your notes`)) setApprovalOpen(false); }}>
                Request changes
              </Button>
              <Button variant="primary" icon="check" busy={busy} onClick={async () => { if (await transition(current.id, "approved", `${current.version} approved — ready to publish`)) setApprovalOpen(false); }}>
                Approve release
              </Button>
            </>
          }
        >
          <div className="col gap-16">
            {current.checklist.some((c) => !c.ok) && (
              <Callout tone="critical" title={`${current.checklist.filter((c) => !c.ok).length} checklist items failing`}>
                Approving now is an override. Add a justification below — it becomes part of the immutable audit record.
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
              <textarea className="textarea" placeholder="e.g. Verified wait-time variable in staging; stale-source re-sync scheduled before publish window." />
            </label>
          </div>
        </Drawer>
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
