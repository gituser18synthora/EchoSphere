import { useState } from "react";
import { useNavigate } from "react-router-dom";
import type { VoiceBot } from "@/types/domain";
import { Icon } from "@/components/Icon";
import { Button, Field, KpiCard, Modal, MultiSelect, Progress, Timeline } from "@/components/ui";
import { fmtNum } from "@/components/charts";
import { useAsync } from "@/hooks/useAsync";
import { listAudit, listLanguages, updateBot } from "@/services/api";
import { useApp } from "@/state/AppContext";

const tabIcons: Record<string, "play" | "edit" | "refresh" | "mic" | "target" | "workflow" | "phone"> = {
  testing: "play", prompts: "edit", knowledge: "refresh", voice: "mic",
  intents: "target", workflows: "workflow", channels: "phone",
};

export default function OverviewTab({ bot, onUpdated }: { bot: VoiceBot; onUpdated?: () => void }) {
  const navigate = useNavigate();
  const [editLangs, setEditLangs] = useState(false);
  const done = bot.readiness.filter((r) => r.done).length;
  const pct = (done / bot.readiness.length) * 100;

  const auditQ = useAsync(listAudit, []);
  const activity = (auditQ.data ?? [])
    .filter((a) => a.entityId === bot.id || a.target.includes(bot.name))
    .slice(0, 5);
  const nextSteps = bot.readiness.filter((r) => !r.done).slice(0, 3);

  return (
    <div className="grid" style={{ gridTemplateColumns: "1.5fr 1fr", gap: 20 }}>
      <div className="col gap-16">
        <div className="card card-pad col gap-8">
          <span className="t-label">Business goal</span>
          <p className="t-body" style={{ fontSize: 14 }}>{bot.description}</p>
          <div className="row gap-16 mt-8 wrap">
            <Meta icon="user" label="Owner" value={bot.owner} />
            <Meta icon="globe" label="Languages" value={bot.languages.join(", ")} onEdit={() => setEditLangs(true)} />
            <Meta icon="target" label="Use case" value={bot.useCase} />
            <Meta icon="clock" label="Updated" value={new Date(bot.updatedAt).toLocaleDateString("en-US", { month: "short", day: "numeric" })} />
          </div>
        </div>

        {bot.status === "published" && (
          <div className="grid grid-4" style={{ gap: 12 }}>
            <KpiCard label="Calls today" value={fmtNum(bot.callsToday)} icon="phone" />
            <KpiCard label="Containment" value={`${bot.containment}%`} icon="check-circle" />
            <KpiCard label="CSAT" value={`${bot.csat.toFixed(1)} / 5`} icon="star" />
            <KpiCard label="Cost / call" value={`$${bot.avgCostPerCall.toFixed(2)}`} icon="dollar" />
          </div>
        )}

        <div className="card">
          <div className="card-header"><span className="card-title">Recent activity</span></div>
          <div style={{ padding: "16px 20px" }}>
            {activity.length === 0 ? (
              <span className="t-sub">No recent activity recorded for this bot.</span>
            ) : (
              <Timeline
                items={activity.map((a) => ({
                  icon: a.action.toLowerCase().includes("publish") ? "rocket" as const
                    : a.action.toLowerCase().includes("archiv") ? "undo" as const
                    : "edit" as const,
                  tone: a.action.toLowerCase().includes("publish") ? "good" as const : "brand" as const,
                  title: <>{a.action} — <b>{a.target}</b></>,
                  meta: `${a.actor} · ${new Date(a.time).toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}`,
                }))}
              />
            )}
          </div>
        </div>
      </div>

      <div className="col gap-16">
        <div className="card">
          <div className="card-header">
            <span className="card-title">Readiness checklist</span>
            <span className="t-micro t-num">{done}/{bot.readiness.length}</span>
          </div>
          <div className="col" style={{ padding: 16, gap: 10 }}>
            <Progress value={pct} tone={pct === 100 ? "good" : pct > 50 ? undefined : "warning"} />
            <div className="col gap-4 mt-8">
              {bot.readiness.map((r) => (
                <button
                  key={r.id}
                  className="row-between"
                  style={{ padding: "7px 8px", borderRadius: 8, textAlign: "left" }}
                  onClick={() => navigate(`/t/bots/${bot.id}/${r.studioTab}`)}
                  onMouseEnter={(e) => (e.currentTarget.style.background = "var(--surface-3)")}
                  onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}
                >
                  <span className="row gap-8" style={{ fontSize: 13 }}>
                    {r.done
                      ? <Icon name="check-circle" size={15} style={{ color: "var(--status-good)" }} />
                      : <Icon name="clock" size={15} style={{ color: "var(--viz-warning)" }} />}
                    <span style={{ color: r.done ? "var(--ink-2)" : "var(--ink)", fontWeight: r.done ? 450 : 600 }}>{r.label}</span>
                  </span>
                  <Icon name="chevron-right" size={13} style={{ color: "var(--ink-3)" }} />
                </button>
              ))}
            </div>
          </div>
        </div>

        <div className="card card-pad col gap-10">
          <span className="card-title">Suggested next steps</span>
          {nextSteps.length === 0 ? (
            <span className="row gap-6 t-sub"><Icon name="check-circle" size={14} style={{ color: "var(--status-good)" }} /> All readiness checks complete</span>
          ) : (
            nextSteps.map((s) => (
              <button key={s.id} className="row gap-10 card-pad-sm" style={{ border: "1px solid var(--hairline)", borderRadius: 10, textAlign: "left" }}
                onClick={() => navigate(`/t/bots/${bot.id}/${s.studioTab}`)}>
                <Icon name={tabIcons[s.studioTab] ?? "edit"} size={14} style={{ color: "var(--brand-500)" }} />
                <span style={{ fontSize: 12.5, fontWeight: 550 }}>{s.label}</span>
              </button>
            ))
          )}
        </div>
      </div>

      {editLangs && (
        <EditLanguagesModal
          bot={bot}
          onClose={() => setEditLangs(false)}
          onSaved={() => { setEditLangs(false); onUpdated?.(); }}
        />
      )}
    </div>
  );
}

function EditLanguagesModal({ bot, onClose, onSaved }: { bot: VoiceBot; onClose: () => void; onSaved: () => void }) {
  const { toast } = useApp();
  const langsQ = useAsync(() => listLanguages(), []);
  const [langs, setLangs] = useState<string[]>(bot.languages);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const save = async () => {
    if (langs.length === 0) { setErr("Select at least one language"); return; }
    setBusy(true);
    try {
      await updateBot(bot.id, { languages: langs });
      toast("Languages updated");
      onSaved();
    } catch (e) {
      toast(e instanceof Error ? e.message : "Failed to update languages", "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open onClose={onClose} title="Edit languages"
      sub="Callers can speak to this bot in any of the selected languages."
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button variant="primary" busy={busy} onClick={save}>Save languages</Button>
        </>
      }
    >
      <Field label="Languages" required plain error={err}>
        <MultiSelect
          options={(langsQ.data ?? []).filter((l) => l.enabled).map((l) => ({
            value: l.code,
            label: l.nativeName && l.nativeName !== l.name ? `${l.name} · ${l.nativeName}` : l.name,
            sub: l.code,
          }))}
          selected={langs}
          onChange={(next) => { setLangs(next); setErr(""); }}
          placeholder="Select supported languages"
          searchPlaceholder="Search languages…"
          invalid={!!err}
        />
      </Field>
    </Modal>
  );
}

function Meta({ icon, label, value, onEdit }: {
  icon: Parameters<typeof Icon>[0]["name"]; label: string; value: string; onEdit?: () => void;
}) {
  return (
    <span className="col" style={{ gap: 2 }}>
      <span className="t-micro row gap-4"><Icon name={icon} size={12} />{label}</span>
      <span className="t-strong row gap-6" style={{ fontSize: 13 }}>
        {value}
        {onEdit && (
          <button className="btn-icon" style={{ width: 22, height: 22 }} aria-label={`Edit ${label.toLowerCase()}`} onClick={onEdit}>
            <Icon name="edit" size={12} />
          </button>
        )}
      </span>
    </span>
  );
}
