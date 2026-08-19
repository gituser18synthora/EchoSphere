import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAsync } from "@/hooks/useAsync";
import { createBot, listBots, listLanguages, simulateAction } from "@/services/api";
import {
  Button, ConfirmModal, Field, Health, MenuButton, Modal, MultiSelect, StatusChip,
  CardSkeleton, EmptyState,
} from "@/components/ui";
import { Icon } from "@/components/Icon";
import { fmtNum } from "@/components/charts";
import { useApp } from "@/state/AppContext";
import type { VoiceBot } from "@/types/domain";

const langSummary = (codes: string[], max = 3) =>
  codes.length <= max ? codes.join(", ") : `${codes.slice(0, max).join(", ")} +${codes.length - max} more`;

export default function Bots() {
  const navigate = useNavigate();
  const { toast, hasPermission } = useApp();
  // Server-enforced (the API nulls avgCostPerCall / rejects bot creation for
  // roles without these permissions); this only removes the affordances.
  const showCosts = hasPermission("costs.view");
  const canManageBots = hasPermission("bots.manage");
  const q = useAsync(listBots, []);
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("all");
  const [view, setView] = useState<"cards" | "table">("cards");
  const [createOpen, setCreateOpen] = useState(false);
  const [archiveTarget, setArchiveTarget] = useState<VoiceBot | null>(null);
  const [rollbackTarget, setRollbackTarget] = useState<VoiceBot | null>(null);
  const [busy, setBusy] = useState(false);

  const rows = useMemo(() => {
    let r = q.data ?? [];
    if (query) {
      const s = query.toLowerCase();
      r = r.filter((b) => b.name.toLowerCase().includes(s) || b.useCase.toLowerCase().includes(s) || b.owner.toLowerCase().includes(s));
    }
    if (status !== "all") r = r.filter((b) => b.status === status);
    return r;
  }, [q.data, query, status]);

  const act = async (label: string, after?: () => void) => {
    setBusy(true);
    await simulateAction(label);
    setBusy(false);
    toast(label);
    after?.();
  };

  const botMenu = (b: VoiceBot) => [
    { label: "Open in Studio", icon: "edit" as const, onClick: () => navigate(`/t/bots/${b.id}/overview`) },
    { label: "Clone bot", icon: "copy" as const, onClick: () => act(`“${b.name}” cloned as draft — knowledge links and prompts copied, channels reset`) },
    { label: "View analytics", icon: "trend" as const, onClick: () => navigate(`/t/bots/${b.id}/analytics`) },
    "sep" as const,
    ...(b.status === "published" && b.liveVersion
      ? [{ label: `Roll back to previous`, icon: "undo" as const, onClick: () => setRollbackTarget(b) }]
      : []),
    ...(b.status === "draft" || b.status === "archived"
      ? []
      : [{ label: "Publish center", icon: "rocket" as const, onClick: () => navigate(`/t/bots/${b.id}/publish`) }]),
    { label: b.status === "archived" ? "Restore" : "Archive", icon: "trash" as const, danger: b.status !== "archived", onClick: () => setArchiveTarget(b) },
  ];

  return (
    <>
      <div className="page-head">
        <div className="page-head-titles">
          <h1 className="page-title">My VoiceBots</h1>
          <p className="page-sub">{q.data ? `${q.data.length} bots · ${q.data.filter((b) => b.status === "published").length} live` : "Loading…"}</p>
        </div>
        <div className="page-actions">
          <div className="segmented" role="group" aria-label="View mode">
            <button aria-pressed={view === "cards"} onClick={() => setView("cards")}>Cards</button>
            <button aria-pressed={view === "table"} onClick={() => setView("table")}>Table</button>
          </div>
          {canManageBots && (
            <Button variant="primary" icon="plus" onClick={() => setCreateOpen(true)}>Create bot</Button>
          )}
        </div>
      </div>

      <div className="filter-bar">
        <div className="search-box">
          <Icon name="search" size={14} />
          <input className="input" placeholder="Search bots, use cases, owners…" value={query} onChange={(e) => setQuery(e.target.value)} aria-label="Search bots" />
        </div>
        <select className="select" value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Filter by status">
          <option value="all">All statuses</option>
          {["published", "in_review", "draft", "rolled_back", "archived"].map((s) => (
            <option key={s} value={s} style={{ textTransform: "capitalize" }}>{s.replace("_", " ")}</option>
          ))}
        </select>
      </div>

      {q.loading && <div className="grid grid-3">{Array.from({ length: 6 }).map((_, i) => <CardSkeleton key={i} rows={4} />)}</div>}

      {!q.loading && rows.length === 0 && (
        <div className="card">
          <EmptyState
            icon="bot"
            title={query || status !== "all" ? "No bots match these filters" : "Create your first VoiceBot"}
            body={query || status !== "all" ? "Adjust the search or status filter." : "A guided setup takes about 10 minutes: name it, add knowledge, pick a voice, then test and publish."}
            action={canManageBots
              ? <Button variant="primary" icon="plus" onClick={() => setCreateOpen(true)}>Create bot</Button>
              : undefined}
          />
        </div>
      )}

      {!q.loading && view === "cards" && rows.length > 0 && (
        <div className="grid grid-3">
          {rows.map((b) => (
            <div key={b.id} className="card card-pad card-clickable col" style={{ gap: 12 }} onClick={() => navigate(`/t/bots/${b.id}/overview`)} role="button" tabIndex={0}
              onKeyDown={(e) => e.key === "Enter" && navigate(`/t/bots/${b.id}/overview`)}>
              <div className="row gap-12">
                <span className="icon-tile brand"><Icon name="bot" size={17} /></span>
                <div className="grow" style={{ minWidth: 0 }}>
                  <div className="t-strong truncate" style={{ fontSize: 14 }}>{b.name}</div>
                  <div className="t-micro truncate">{b.useCase}</div>
                </div>
                <div onClick={(e) => e.stopPropagation()}><MenuButton actions={botMenu(b)} /></div>
              </div>
              <div className="row gap-6 wrap">
                <StatusChip status={b.status} />
                <Health level={b.health} />
                <span className="tag t-num">{b.liveVersion ?? b.version}</span>
              </div>
              <div className="row" style={{ gap: 0, borderTop: "1px solid var(--hairline)", paddingTop: 12, justifyContent: "space-between" }}>
                <Stat label="Calls /mo" value={b.callsMonth ? fmtNum(b.callsMonth) : "—"} />
                <Stat label="Contained" value={b.containment ? `${b.containment}%` : "—"} />
                <Stat label="CSAT" value={b.csat ? b.csat.toFixed(1) : "—"} />
                {showCosts && <Stat label="$/call" value={b.avgCostPerCall ? `$${b.avgCostPerCall.toFixed(2)}` : "—"} />}
              </div>
              <div className="row-between t-micro">
                <span className="row gap-4"><Icon name="user" size={12} />{b.owner}</span>
                <span title={b.languages.join(", ")}>{langSummary(b.languages)}</span>
              </div>
            </div>
          ))}
        </div>
      )}

      {!q.loading && view === "table" && rows.length > 0 && (
        <div className="card">
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Bot</th><th>Status</th><th>Health</th><th>Version</th><th>Owner</th>
                  <th>Languages</th><th className="num">Calls /mo</th><th className="num">Contained</th>{showCosts && <th className="num">$/call</th>}<th></th>
                </tr>
              </thead>
              <tbody>
                {rows.map((b) => (
                  <tr key={b.id} className="row-click" onClick={() => navigate(`/t/bots/${b.id}/overview`)}>
                    <td><div className="t-strong">{b.name}</div><div className="t-micro">{b.useCase}</div></td>
                    <td><StatusChip status={b.status} /></td>
                    <td><Health level={b.health} /></td>
                    <td><code>{b.liveVersion ?? b.version}</code></td>
                    <td className="t-sub">{b.owner}</td>
                    <td className="t-sub" title={b.languages.join(", ")}>{langSummary(b.languages)}</td>
                    <td className="num t-num">{b.callsMonth ? fmtNum(b.callsMonth) : "—"}</td>
                    <td className="num t-num">{b.containment ? `${b.containment}%` : "—"}</td>
                    {showCosts && <td className="num t-num">{b.avgCostPerCall ? `$${b.avgCostPerCall.toFixed(2)}` : "—"}</td>}
                    <td onClick={(e) => e.stopPropagation()}><MenuButton actions={botMenu(b)} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <CreateBotModal open={createOpen} onClose={() => setCreateOpen(false)} onCreated={q.reload} />

      <ConfirmModal
        open={!!archiveTarget}
        onClose={() => setArchiveTarget(null)}
        danger={archiveTarget?.status !== "archived"}
        busy={busy}
        title={archiveTarget?.status === "archived" ? `Restore ${archiveTarget?.name}?` : `Archive ${archiveTarget?.name}?`}
        confirmLabel={archiveTarget?.status === "archived" ? "Restore bot" : "Archive bot"}
        body={
          archiveTarget?.status === "archived"
            ? "The bot returns to draft state. Channels must be re-tested before publishing again."
            : <>The bot stops receiving new calls immediately. Configuration, knowledge and version history are <b>retained</b> and it can be restored at any time. This is recorded in the audit log.</>
        }
        onConfirm={() => act(archiveTarget!.status === "archived" ? `${archiveTarget!.name} restored to draft` : `${archiveTarget!.name} archived — no new calls will be routed`, () => setArchiveTarget(null))}
      />

      <ConfirmModal
        open={!!rollbackTarget}
        onClose={() => setRollbackTarget(null)}
        danger
        busy={busy}
        title={`Roll back ${rollbackTarget?.name}?`}
        confirmLabel="Roll back now"
        body={
          <>
            Live traffic switches from <code>{rollbackTarget?.liveVersion}</code> back to the previous published
            version within 60 seconds. In-progress calls finish on the current version. A rollback release entry
            is created and the team is notified.
          </>
        }
        onConfirm={() => act(`${rollbackTarget?.name} rolled back — traffic switching to previous version`, () => setRollbackTarget(null))}
      />
    </>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="col" style={{ gap: 1 }}>
      <span className="t-micro">{label}</span>
      <span className="t-strong t-num" style={{ fontSize: 13.5 }}>{value}</span>
    </div>
  );
}

function CreateBotModal({ open, onClose, onCreated }: { open: boolean; onClose: () => void; onCreated: () => void }) {
  const navigate = useNavigate();
  const { toast } = useApp();
  const langsQ = useAsync(listLanguages, []);
  const [name, setName] = useState("");
  const [useCase, setUseCase] = useState("Appointment booking");
  const [langs, setLangs] = useState<string[]>([]);
  const [err, setErr] = useState("");
  const [langErr, setLangErr] = useState("");
  const [busy, setBusy] = useState(false);

  /* The platform catalog is the source of truth. Never keep a locale merely
     because it used to be the product default: an administrator can disable
     it at any time. Prefer the enabled Platform default, then catalog order. */
  useEffect(() => {
    if (!open || !langsQ.data) return;
    const enabled = langsQ.data.filter((language) => language.enabled);
    const enabledCodes = new Set(enabled.map((language) => language.code));
    setLangs((current) => {
      const valid = current.filter((code) => enabledCodes.has(code));
      if (valid.length) return valid;
      const preferred = enabled.find((language) => language.isDefault) ?? enabled[0];
      return preferred ? [preferred.code] : [];
    });
  }, [open, langsQ.data]);

  const create = async () => {
    if (name.trim().length < 3) { setErr("Give the bot a name (at least 3 characters)"); return; }
    if (langs.length === 0) { setLangErr("Select at least one language"); return; }
    setBusy(true);
    try {
      const created = await createBot({ name: name.trim(), useCase, languages: langs });
      toast("VoiceBot created");
      onCreated();
      onClose();
      navigate(`/t/bots/${created.id}`);
    } catch (e) {
      toast(e instanceof Error ? e.message : "Failed to create bot", "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open={open} onClose={onClose} title="Create a VoiceBot"
      sub="Starts as a draft — nothing goes live until it passes review and you publish."
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button variant="primary" icon="bot" busy={busy} onClick={create}>Create draft</Button>
        </>
      }
    >
      <div className="col gap-16">
        <Field label="Bot name" required error={err}>
          <input className="input" value={name} autoFocus onChange={(e) => { setName(e.target.value); setErr(""); }} placeholder="e.g. Appointment Concierge" aria-invalid={!!err} />
        </Field>
        <Field label="Primary use case" hint="Preloads a matching workflow template and readiness checklist.">
          <select className="select" value={useCase} onChange={(e) => setUseCase(e.target.value)}>
            {["Appointment booking", "Billing support", "Order status", "FAQ & information", "Triage & routing", "Surveys & feedback", "Custom"].map((u) => <option key={u}>{u}</option>)}
          </select>
        </Field>
        <Field label="Languages" required plain error={langErr} hint="Callers can speak to the bot in any of these.">
          <MultiSelect
            options={(langsQ.data ?? []).filter((l) => l.enabled).map((l) => ({
              value: l.code,
              label: l.nativeName && l.nativeName !== l.name ? `${l.name} · ${l.nativeName}` : l.name,
              sub: l.code,
            }))}
            selected={langs}
            onChange={(next) => { setLangs(next); setLangErr(""); }}
            placeholder="Select supported languages"
            searchPlaceholder="Search languages…"
            invalid={!!langErr}
          />
        </Field>
      </div>
    </Modal>
  );
}
