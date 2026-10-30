import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAsync } from "@/hooks/useAsync";
import {
  archiveBot, cloneBot, createBot, deleteBot, listBots, listLanguages, restoreBot, simulateAction,
} from "@/services/api";
import {
  Button, ConfirmModal, Field, Health, MenuButton, Modal, MultiSelect, StatusChip, TypedConfirmModal,
  CardSkeleton, EmptyState,
} from "@/components/ui";
import { Icon } from "@/components/Icon";
import { fmtNum } from "@/components/charts";
import { useApp } from "@/state/AppContext";
import { downloadBotPackage } from "@/services/botTransfer";
import { ImportBotModal } from "@/pages/tenant/ImportBotModal";
import type { VoiceBot } from "@/types/domain";

const langSummary = (codes: string[], max = 3) =>
  codes.length <= max ? codes.join(", ") : `${codes.slice(0, max).join(", ")} +${codes.length - max} more`;

/* Status filter. "active" (the default) is every bot that is not archived.
   Archived is a normal, recoverable management state — those bots live under
   their own filter so the working list stays uncluttered, and a deleted bot is
   never returned by the API at all. */
const STATUS_FILTERS: { value: string; label: string }[] = [
  { value: "active", label: "All active" },
  { value: "published", label: "Published" },
  { value: "in_review", label: "In review" },
  { value: "approved", label: "Approved" },
  { value: "draft", label: "Draft" },
  { value: "rolled_back", label: "Rolled back" },
  { value: "archived", label: "Archived" },
];

type LifecycleAction = "archive" | "restore";

export default function Bots() {
  const navigate = useNavigate();
  const { toast, hasPermission, user } = useApp();
  // Server-enforced (the API nulls avgCostPerCall / rejects bot creation for
  // roles without these permissions); this only removes the affordances.
  const showCosts = hasPermission("costs.view");
  const canManageBots = hasPermission("bots.manage");
  const q = useAsync(listBots, []);
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("active");
  const [view, setView] = useState<"cards" | "table">("cards");
  const [createOpen, setCreateOpen] = useState(false);
  const [lifecycle, setLifecycle] = useState<{ bot: VoiceBot; action: LifecycleAction } | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<VoiceBot | null>(null);
  const [rollbackTarget, setRollbackTarget] = useState<VoiceBot | null>(null);
  const [busy, setBusy] = useState(false);
  const [cloningId, setCloningId] = useState<string | null>(null);
  const [exportingId, setExportingId] = useState<string | null>(null);
  const [importOpen, setImportOpen] = useState(false);

  const rows = useMemo(() => {
    let r = q.data ?? [];
    if (query) {
      const s = query.toLowerCase();
      r = r.filter((b) => b.name.toLowerCase().includes(s) || b.useCase.toLowerCase().includes(s) || b.owner.toLowerCase().includes(s));
    }
    r = status === "active" ? r.filter((b) => b.status !== "archived") : r.filter((b) => b.status === status);
    return r;
  }, [q.data, query, status]);

  const counts = useMemo(() => {
    const all = q.data ?? [];
    return {
      active: all.filter((b) => b.status !== "archived").length,
      live: all.filter((b) => b.status === "published").length,
      archived: all.filter((b) => b.status === "archived").length,
    };
  }, [q.data]);

  const act = async (label: string, after?: () => void) => {
    setBusy(true);
    await simulateAction(label);
    setBusy(false);
    toast(label);
    after?.();
  };

  /* Archive and Restore are the reversible pair: archive parks the bot (status
     "archived", channels deactivated, phone number reserved, everything kept);
     restore returns it to draft. On failure the modal stays open so the action
     can be retried or cancelled. */
  const confirmLifecycle = async () => {
    const target = lifecycle;
    if (!target || busy) return;
    setBusy(true);
    try {
      if (target.action === "restore") {
        await restoreBot(target.bot.id);
        toast(`${target.bot.name} restored as a draft — publish it again before it takes calls`);
      } else {
        await archiveBot(target.bot.id);
        toast(`${target.bot.name} archived — no calls, messages or test sessions until restored`);
      }
      setLifecycle(null);
      q.reload();
    } catch (e) {
      toast(e instanceof Error ? e.message : `Failed to ${target.action} bot`, "error");
    } finally {
      setBusy(false);
    }
  };

  /* Delete is permanent: the bot leaves every list (Archived included) and has
     no Restore. The modal makes the user type the bot name first. */
  const confirmDelete = async () => {
    const target = deleteTarget;
    if (!target || busy) return;
    setBusy(true);
    try {
      await deleteBot(target.id);
      toast(`${target.name} deleted permanently`);
      setDeleteTarget(null);
      q.reload();
    } catch (e) {
      toast(e instanceof Error ? e.message : "Failed to delete bot", "error");
    } finally {
      setBusy(false);
    }
  };

  const clone = async (b: VoiceBot) => {
    if (cloningId) return; // one clone at a time — repeat clicks must not fork duplicates
    setCloningId(b.id);
    try {
      const created = await cloneBot(b.id);
      toast(`“${b.name}” cloned as “${created.name}” — draft, channels not copied`);
      q.reload();
    } catch (e) {
      toast(e instanceof Error ? e.message : "Failed to clone bot", "error");
    } finally {
      setCloningId(null);
    }
  };

  /* Export downloads the bot as one bot_<id>.json — the artifact for
     "Import bot" on another environment (same tenant, same bot id). */
  const exportBot = async (b: VoiceBot) => {
    if (exportingId) return;
    setExportingId(b.id);
    try {
      const { filename } = await downloadBotPackage(b.id);
      toast(`“${b.name}” exported as ${filename} — import it on the target environment under the same tenant`);
    } catch (e) {
      toast(e instanceof Error ? e.message : "Failed to export bot", "error");
    } finally {
      setExportingId(null);
    }
  };

  const botMenu = (b: VoiceBot) => {
    const archived = b.status === "archived";
    return [
      { label: "Open in Studio", icon: "edit" as const, onClick: () => navigate(`/t/bots/${b.id}/overview`) },
      ...(canManageBots
        ? [{
            label: cloningId === b.id ? "Cloning…" : "Clone bot",
            icon: "copy" as const,
            disabled: cloningId !== null,
            onClick: () => clone(b),
          }]
        : []),
      { label: "View analytics", icon: "trend" as const, onClick: () => navigate(`/t/bots/${b.id}/analytics`) },
      ...(canManageBots
        ? [{
            label: exportingId === b.id ? "Exporting…" : "Export bot JSON",
            icon: "download" as const,
            disabled: exportingId !== null,
            onClick: () => exportBot(b),
          }]
        : []),
      "sep" as const,
      ...(b.status === "published" && b.liveVersion
        ? [{ label: `Roll back to previous`, icon: "undo" as const, onClick: () => setRollbackTarget(b) }]
        : []),
      ...(b.status === "draft" || archived
        ? []
        : [{ label: "Publish center", icon: "rocket" as const, onClick: () => navigate(`/t/bots/${b.id}/publish`) }]),
      ...(canManageBots
        ? [
            archived
              ? { label: "Restore", icon: "undo" as const, onClick: () => setLifecycle({ bot: b, action: "restore" }) }
              : { label: "Archive", icon: "pause" as const, onClick: () => setLifecycle({ bot: b, action: "archive" }) },
            { label: "Delete", icon: "trash" as const, danger: true, onClick: () => setDeleteTarget(b) },
          ]
        : []),
    ];
  };

  const emptyTitle = status === "archived" && !query
    ? "No archived bots"
    : query || status !== "active" ? "No bots match these filters" : "Create your first VoiceBot";
  const emptyBody = status === "archived" && !query
    ? "Archived bots keep their whole configuration and can be restored at any time."
    : query || status !== "active"
      ? "Adjust the search or status filter."
      : "A guided setup takes about 10 minutes: name it, add knowledge, pick a voice, then test and publish.";

  return (
    <>
      <div className="page-head">
        <div className="page-head-titles">
          <h1 className="page-title">My VoiceBots</h1>
          <p className="page-sub">
            {q.data
              ? `${counts.active} active · ${counts.live} live${counts.archived ? ` · ${counts.archived} archived` : ""}`
              : "Loading…"}
          </p>
        </div>
        <div className="page-actions">
          <div className="segmented" role="group" aria-label="View mode">
            <button aria-pressed={view === "cards"} onClick={() => setView("cards")}>Cards</button>
            <button aria-pressed={view === "table"} onClick={() => setView("table")}>Table</button>
          </div>
          {canManageBots && (
            <Button icon="upload" onClick={() => setImportOpen(true)}>Import bot</Button>
          )}
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
          {STATUS_FILTERS.map((f) => <option key={f.value} value={f.value}>{f.label}</option>)}
        </select>
      </div>

      {q.loading && <div className="grid grid-3">{Array.from({ length: 6 }).map((_, i) => <CardSkeleton key={i} rows={4} />)}</div>}

      {!q.loading && rows.length === 0 && (
        <div className="card">
          <EmptyState
            icon="bot"
            title={emptyTitle}
            body={emptyBody}
            action={canManageBots && status === "active" && !query
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

      <ImportBotModal
        open={importOpen}
        onClose={() => setImportOpen(false)}
        tenantId={user?.tenantId}
        onImported={(report) => {
          toast(report.action === "update"
            ? `“${report.botName ?? report.botId}” updated from the imported package`
            : `“${report.botName ?? report.botId}” created from the imported package`);
          q.reload();
        }}
      />

      <ConfirmModal
        open={!!lifecycle}
        onClose={() => setLifecycle(null)}
        busy={busy}
        title={lifecycle?.action === "restore" ? `Restore ${lifecycle.bot.name}?` : `Archive ${lifecycle?.bot.name}?`}
        confirmLabel={lifecycle?.action === "restore" ? "Restore bot" : "Archive bot"}
        body={
          lifecycle?.action === "restore"
            ? <>The bot returns to <b>Draft</b> with its workflows, prompts, knowledge, intents, voice settings and tests exactly as they were, and keeps its phone number. Channels stay deactivated until they are re-tested, and nothing goes live until you publish it again.</>
            : <>The bot stops taking calls, messages and test sessions immediately. Its channels are deactivated and its phone number is <b>reserved</b> for this bot. All configuration and history are kept — you can restore it at any time. This is recorded in the audit log.</>
        }
        onConfirm={confirmLifecycle}
      />

      <TypedConfirmModal
        open={!!deleteTarget}
        onClose={() => setDeleteTarget(null)}
        busy={busy}
        title={`Delete ${deleteTarget?.name ?? "bot"} permanently?`}
        confirmLabel="Delete bot permanently"
        confirmText={deleteTarget?.name ?? ""}
        body={
          <>
            <p style={{ marginTop: 0 }}>
              This <b>cannot be undone</b> — a deleted bot has no Restore action and disappears from every list, including Archived.
              If you might need it again, archive it instead.
            </p>
            <ul style={{ margin: 0, paddingLeft: 18 }}>
              <li>Active channels are removed and disabled; webhooks stop accepting traffic.</li>
              <li>Its phone number is released back to the platform pool.</li>
              <li>Workflows, prompts, intents, knowledge sources and test scenarios are removed from the workspace.</li>
              <li>Conversation history, transcripts and usage records are kept for reporting and audit.</li>
            </ul>
          </>
        }
        onConfirm={confirmDelete}
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
