import { useMemo, useState } from "react";
import type {
  ApiConnection, EntityDef, EntityExtraction, Intent, IntentTestResult, VoiceBot, Workflow,
} from "@/types/domain";
import { useAsync } from "@/hooks/useAsync";
import {
  createEntity, createIntent, deleteEntity, deleteIntent, duplicateEntity, duplicateIntent,
  listApis, listEntities, listIntents, listLanguages, listWorkflows,
  testEntity, testIntents, updateEntity, updateIntent,
} from "@/services/api";
import {
  Button, Callout, ConfirmModal, Drawer, Field, MenuButton, Progress, StatusChip, Tabs, Toggle,
} from "@/components/ui";
import { DataTable } from "@/components/DataTable";
import { Icon } from "@/components/Icon";
import { useApp } from "@/state/AppContext";

/* ---------- helpers ---------- */

const suggestCode = (name: string) =>
  name.trim().toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");

const normalizePhrase = (s: string) => s.trim().toLowerCase().replace(/\s+/g, " ");

const DATA_TYPES = [
  "text", "number", "integer", "decimal", "date", "date_range", "time", "duration",
  "currency", "percentage", "phone", "email", "account_number", "policy_number",
  "claim_number", "card_last4", "person_name", "location", "product", "list", "regex", "api",
] as const;

const FALLBACK_BEHAVIORS = [
  { value: "clarify", label: "Clarify — ask a follow-up question" },
  { value: "handoff", label: "Handoff — route to a human" },
  { value: "llm", label: "LLM — let the model answer" },
];

type LanguageOption = { code: string; name: string };

const errMsg = (e: unknown, fallback: string) => (e instanceof Error ? e.message : fallback);

/* ---------- shared small components ---------- */

function CheckList({ options, selected, onToggle, emptyText, ariaLabel }: {
  options: { value: string; label: string }[];
  selected: string[];
  onToggle: (v: string) => void;
  emptyText: string;
  ariaLabel: string;
}) {
  if (!options.length) return <span className="t-micro">{emptyText}</span>;
  return (
    <div
      className="col gap-4" role="group" aria-label={ariaLabel}
      style={{ maxHeight: 150, overflowY: "auto", border: "1px solid var(--hairline)", borderRadius: 8, padding: 8 }}
    >
      {options.map((o) => (
        <label key={o.value} className="row gap-8" style={{ fontSize: 12.5, cursor: "pointer" }}>
          <input type="checkbox" checked={selected.includes(o.value)} onChange={() => onToggle(o.value)} />
          {o.label}
        </label>
      ))}
    </div>
  );
}

function ChipsInput({ values, onChange, placeholder, ariaLabel }: {
  values: string[]; onChange: (v: string[]) => void; placeholder?: string; ariaLabel: string;
}) {
  const [input, setInput] = useState("");
  const add = () => {
    const v = input.trim();
    if (v && !values.includes(v)) onChange([...values, v]);
    setInput("");
  };
  return (
    <div className="col gap-6">
      {values.length > 0 && (
        <div className="row gap-6 wrap">
          {values.map((v) => (
            <span key={v} className="chip chip-neutral">
              {v}
              <button
                type="button" aria-label={`Remove ${v}`}
                style={{ display: "inline-flex", background: "none", border: 0, cursor: "pointer", color: "inherit", padding: 0 }}
                onClick={() => onChange(values.filter((x) => x !== v))}
              >
                <Icon name="x" size={10} />
              </button>
            </span>
          ))}
        </div>
      )}
      <input
        className="input" value={input} placeholder={placeholder} aria-label={ariaLabel}
        onChange={(e) => setInput(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); add(); } }}
      />
    </div>
  );
}

function LanguageChips({ options, selected, onToggle }: {
  options: LanguageOption[]; selected: string[]; onToggle: (code: string) => void;
}) {
  if (!options.length) return <span className="t-micro">No languages configured</span>;
  return (
    <div className="row gap-6 wrap" role="group" aria-label="Languages">
      {options.map((l) => {
        const on = selected.includes(l.code);
        return (
          <button
            key={l.code} type="button" aria-pressed={on}
            className={`chip ${on ? "chip-brand" : "chip-neutral"}`}
            style={{ cursor: "pointer", border: 0 }}
            onClick={() => onToggle(l.code)}
          >
            {on && <Icon name="check" size={10} />}
            {l.name} ({l.code})
          </button>
        );
      })}
    </div>
  );
}

/* ============================================================ */

export default function IntentsTab({ bot }: { bot: VoiceBot }) {
  const [sub, setSub] = useState("intents");
  const intentsQ = useAsync(() => listIntents(bot.id), [bot.id]);
  const entitiesQ = useAsync(() => listEntities(), []);
  const workflowsQ = useAsync(listWorkflows, []);
  const apisQ = useAsync(() => listApis(bot.id), [bot.id]);
  const languagesQ = useAsync(() => listLanguages(), []);
  const { toast, hasPermission } = useApp();

  const canIntents = hasPermission("manage_intents") || hasPermission("bots.manage");
  const canEntities = hasPermission("manage_entities") || hasPermission("bots.manage");
  const intentPermTitle = canIntents ? undefined : "Requires the manage_intents permission";
  const entityPermTitle = canEntities ? undefined : "Requires the manage_entities permission";

  /* Intent state */
  const [creatingIntent, setCreatingIntent] = useState(false);
  const [editingIntent, setEditingIntent] = useState<Intent | null>(null);
  const [archivingIntent, setArchivingIntent] = useState<Intent | null>(null);
  const [intentActionError, setIntentActionError] = useState<string | null>(null);
  const [archiveBusy, setArchiveBusy] = useState(false);
  const [archiveError, setArchiveError] = useState<string | null>(null);

  /* Entity state */
  const [creatingEntity, setCreatingEntity] = useState(false);
  const [editingEntity, setEditingEntity] = useState<EntityDef | null>(null);
  const [deletingEntity, setDeletingEntity] = useState<EntityDef | null>(null);
  const [entityActionError, setEntityActionError] = useState<string | null>(null);
  const [entityDeleteBusy, setEntityDeleteBusy] = useState(false);
  const [entityDeleteError, setEntityDeleteError] = useState<string | null>(null);

  const languageOptions: LanguageOption[] = useMemo(() => {
    if (languagesQ.data?.length) return languagesQ.data.map((l) => ({ code: l.code, name: l.name }));
    return bot.languages.map((code) => ({ code, name: code }));
  }, [languagesQ.data, bot.languages]);

  const intentRowAction = async (fn: () => Promise<unknown>, success: string) => {
    setIntentActionError(null);
    try {
      await fn();
      toast(success);
      intentsQ.reload();
    } catch (e) {
      setIntentActionError(errMsg(e, "Action failed"));
    }
  };

  const entityRowAction = async (fn: () => Promise<unknown>, success: string) => {
    setEntityActionError(null);
    try {
      await fn();
      toast(success);
      entitiesQ.reload();
    } catch (e) {
      setEntityActionError(errMsg(e, "Action failed"));
    }
  };

  const confirmArchiveIntent = async () => {
    if (!archivingIntent) return;
    setArchiveBusy(true);
    setArchiveError(null);
    try {
      await deleteIntent(archivingIntent.id);
      toast(`Intent “${archivingIntent.name}” archived`);
      setArchivingIntent(null);
      intentsQ.reload();
    } catch (e) {
      setArchiveError(errMsg(e, "Could not archive intent"));
    } finally {
      setArchiveBusy(false);
    }
  };

  const confirmDeleteEntity = async () => {
    if (!deletingEntity) return;
    setEntityDeleteBusy(true);
    setEntityDeleteError(null);
    try {
      await deleteEntity(deletingEntity.id);
      toast(`Entity “${deletingEntity.name}” deleted`);
      setDeletingEntity(null);
      entitiesQ.reload();
    } catch (e) {
      setEntityDeleteError(errMsg(e, "Could not delete entity"));
    } finally {
      setEntityDeleteBusy(false);
    }
  };

  return (
    <div className="col gap-16">
      <Tabs
        tabs={[
          { id: "intents", label: "Intents", count: intentsQ.data?.length },
          { id: "entities", label: "Entities", count: entitiesQ.data?.length },
        ]}
        active={sub}
        onChange={setSub}
      />

      {sub === "intents" && (
        <>
          <div className="row-between">
            <span className="t-sub">Utterance → intent routing. Confidence below threshold triggers the fallback behavior, twice in a row triggers handover.</span>
            <Button variant="primary" size="sm" icon="plus" disabled={!canIntents} title={intentPermTitle} onClick={() => setCreatingIntent(true)}>
              New intent
            </Button>
          </div>

          {intentActionError && <Callout tone="critical" title="Action failed">{intentActionError}</Callout>}

          <div className="card">
            <DataTable
              loading={intentsQ.loading} error={intentsQ.error} onRetry={intentsQ.reload} rows={intentsQ.data}
              onRowClick={(i) => setEditingIntent(i)}
              empty={{ icon: "target", title: "No intents yet", body: "Define what callers can ask for. Each intent routes to a workflow, a knowledge answer or a human." }}
              columns={[
                { key: "name", header: "Intent", sortValue: (i) => i.name, render: (i) => <div><code className="t-strong" style={{ fontSize: 12.5 }}>{i.name}</code><div className="t-micro">{i.description}</div></div> },
                { key: "samples", header: "Samples", align: "right", sortValue: (i) => i.samples.length, render: (i) => <span className="t-num">{i.samples.length}</span> },
                {
                  key: "conf", header: "Avg confidence (30d)", width: 170, sortValue: (i) => i.avgConfidence30d,
                  render: (i) => {
                    const below = i.avgConfidence30d < i.confidenceThreshold;
                    return (
                      <div className="row gap-8">
                        <Progress value={i.avgConfidence30d * 100} tone={below ? "critical" : i.avgConfidence30d < i.confidenceThreshold + 0.1 ? "warning" : "good"} />
                        <span className="t-num t-micro">{(i.avgConfidence30d * 100).toFixed(0)}%</span>
                      </div>
                    );
                  },
                },
                { key: "route", header: "Routes to", render: (i) => <span className="tag">{i.route}</span> },
                { key: "tests", header: "Tests", align: "right", render: (i) => <span className={`t-num ${i.testPass < i.testTotal ? "t-bad" : "t-good"}`} style={{ fontWeight: 600 }}>{i.testPass}/{i.testTotal}</span> },
                { key: "status", header: "Status", render: (i) => <StatusChip status={i.status} /> },
                {
                  key: "actions", header: "", width: 48,
                  render: (i) => (
                    <MenuButton
                      label={`Actions for ${i.name}`}
                      actions={[
                        { label: "Edit", icon: "edit", onClick: () => setEditingIntent(i) },
                        { label: "Duplicate", icon: "copy", disabled: !canIntents, onClick: () => intentRowAction(() => duplicateIntent(i.id), `Intent “${i.name}” duplicated`) },
                        {
                          label: i.status === "disabled" ? "Activate" : "Deactivate",
                          icon: i.status === "disabled" ? "check-circle" : "pause",
                          disabled: !canIntents,
                          onClick: () => intentRowAction(
                            () => updateIntent(i.id, { status: i.status === "disabled" ? "active" : "disabled" }),
                            i.status === "disabled" ? `Intent “${i.name}” activated` : `Intent “${i.name}” deactivated`,
                          ),
                        },
                        "sep",
                        { label: "Archive", icon: "trash", danger: true, disabled: !canIntents, onClick: () => { setArchiveError(null); setArchivingIntent(i); } },
                      ]}
                    />
                  ),
                },
              ]}
            />
          </div>

          <IntentTestConsole
            botId={bot.id}
            defaultLanguage={bot.languages[0] ?? "en-US"}
            languages={languageOptions}
            workflows={workflowsQ.data ?? []}
            apis={apisQ.data ?? []}
          />
        </>
      )}

      {sub === "entities" && (
        <>
          <div className="row-between">
            <span className="t-sub">Structured values the bot extracts from utterances — dates, account numbers, locations.</span>
            <Button variant="primary" size="sm" icon="plus" disabled={!canEntities} title={entityPermTitle} onClick={() => setCreatingEntity(true)}>
              New entity
            </Button>
          </div>
          <Callout tone="warning" title="PII handling">
            Entities marked <b>PII</b> are redacted from stored transcripts and logs by a platform guardrail. Extracted values are used in-call only.
          </Callout>

          {entityActionError && <Callout tone="critical" title="Action failed">{entityActionError}</Callout>}

          <div className="card">
            <DataTable
              loading={entitiesQ.loading} error={entitiesQ.error} onRetry={entitiesQ.reload} rows={entitiesQ.data}
              onRowClick={(e) => setEditingEntity(e)}
              empty={{ icon: "layers", title: "No entities", body: "Create entities so intents can extract structured values from what callers say." }}
              columns={[
                { key: "name", header: "Entity", sortValue: (e) => e.name, render: (e) => <div><code className="t-strong" style={{ fontSize: 12.5 }}>{e.name}</code>{e.description && <div className="t-micro">{e.description}</div>}</div> },
                { key: "kind", header: "Kind", sortValue: (e) => e.kind, render: (e) => <span className="tag" style={{ textTransform: "capitalize" }}>{e.kind}</span> },
                { key: "dataType", header: "Data type", sortValue: (e) => e.dataType ?? "", render: (e) => e.dataType ? <code style={{ fontSize: 12 }}>{e.dataType}</code> : <span className="t-micro">—</span> },
                {
                  key: "rule", header: "Extraction rule / example",
                  render: (e) => (
                    <span className="t-sub" style={{ fontSize: 12.5 }}>
                      {e.regexPattern ? <code style={{ fontSize: 11.5 }}>/{e.regexPattern}/</code>
                        : e.allowedValues?.length ? `${e.allowedValues.length} allowed values`
                        : e.example}
                    </span>
                  ),
                },
                { key: "pii", header: "PII", render: (e) => e.pii ? <StatusChip status="warning" label="PII — redacted" /> : <span className="t-micro">—</span> },
                { key: "status", header: "Status", sortValue: (e) => e.status ?? "active", render: (e) => <StatusChip status={e.status ?? "active"} /> },
                { key: "used", header: "Used by", align: "right", sortValue: (e) => e.usedBy.length, render: (e) => <span className="t-num" title={e.usedBy.join(", ") || "Not used by any intent"}>{e.usedBy.length}</span> },
                {
                  key: "actions", header: "", width: 48,
                  render: (e) => (
                    <MenuButton
                      label={`Actions for ${e.name}`}
                      actions={[
                        { label: "Edit", icon: "edit", onClick: () => setEditingEntity(e) },
                        { label: "Duplicate", icon: "copy", disabled: !canEntities, onClick: () => entityRowAction(() => duplicateEntity(e.id), `Entity “${e.name}” duplicated`) },
                        "sep",
                        { label: "Delete", icon: "trash", danger: true, disabled: !canEntities, onClick: () => { setEntityDeleteError(null); setDeletingEntity(e); } },
                      ]}
                    />
                  ),
                },
              ]}
            />
          </div>
        </>
      )}

      {(creatingIntent || editingIntent) && (
        <IntentFormDrawer
          key={editingIntent?.id ?? "new-intent"}
          botId={bot.id}
          intent={editingIntent}
          entities={entitiesQ.data ?? []}
          workflows={workflowsQ.data ?? []}
          apis={apisQ.data ?? []}
          languages={languageOptions}
          canManage={canIntents}
          permTitle={intentPermTitle}
          onClose={() => { setCreatingIntent(false); setEditingIntent(null); }}
          onSaved={intentsQ.reload}
        />
      )}

      {(creatingEntity || editingEntity) && (
        <EntityFormDrawer
          key={editingEntity?.id ?? "new-entity"}
          entity={editingEntity}
          canManage={canEntities}
          permTitle={entityPermTitle}
          onClose={() => { setCreatingEntity(false); setEditingEntity(null); }}
          onSaved={entitiesQ.reload}
        />
      )}

      <ConfirmModal
        open={!!archivingIntent}
        onClose={() => setArchivingIntent(null)}
        onConfirm={confirmArchiveIntent}
        title="Archive intent?"
        confirmLabel="Archive" danger busy={archiveBusy}
        body={
          <div className="col gap-8">
            {archiveError && <Callout tone="critical">{archiveError}</Callout>}
            <span>
              <code>{archivingIntent?.name}</code> will stop matching caller utterances immediately.
              Conversations that relied on it fall back to the default behavior.
            </span>
          </div>
        }
      />

      <ConfirmModal
        open={!!deletingEntity}
        onClose={() => setDeletingEntity(null)}
        onConfirm={confirmDeleteEntity}
        title="Delete entity?"
        confirmLabel="Delete" danger busy={entityDeleteBusy}
        body={
          <div className="col gap-8">
            {entityDeleteError && <Callout tone="critical">{entityDeleteError}</Callout>}
            <span>
              <code>{deletingEntity?.name}</code> will no longer be extracted.
              Deletion is blocked while intents still reference this entity.
            </span>
          </div>
        }
      />
    </div>
  );
}

/* ============================================================
   Intent test console — real routing via testIntents()
   ============================================================ */

function IntentTestConsole({ botId, defaultLanguage, languages, workflows, apis }: {
  botId: string;
  defaultLanguage: string;
  languages: LanguageOption[];
  workflows: Workflow[];
  apis: ApiConnection[];
}) {
  const [utterance, setUtterance] = useState("");
  const [language, setLanguage] = useState(defaultLanguage);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<IntentTestResult | null>(null);

  const run = async () => {
    if (!utterance.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      setResult(await testIntents(botId, utterance.trim(), language));
    } catch (e) {
      setResult(null);
      setError(errMsg(e, "Intent test failed"));
    } finally {
      setBusy(false);
    }
  };

  const workflowName = result?.workflowId ? (workflows.find((w) => w.id === result.workflowId)?.name ?? result.workflowId) : null;
  const apiName = result?.apiConnectionId ? (apis.find((a) => a.id === result.apiConnectionId)?.name ?? result.apiConnectionId) : null;

  return (
    <div className="card">
      <div className="card-header">
        <div className="col gap-2">
          <span className="card-title">Test console</span>
          <span className="t-micro">Runs the real router — intent matching, entity extraction and routing decision.</span>
        </div>
      </div>
      <div className="col gap-12" style={{ padding: 18 }}>
        <div className="row gap-8">
          <input
            className="input grow" value={utterance} aria-label="Test utterance"
            placeholder="e.g. can I come in Thursday afternoon?"
            onChange={(e) => setUtterance(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && run()}
          />
          <select className="select" style={{ width: 180 }} value={language} aria-label="Test language" onChange={(e) => setLanguage(e.target.value)}>
            {languages.map((l) => <option key={l.code} value={l.code}>{l.name} ({l.code})</option>)}
          </select>
          <Button variant="primary" icon="play" busy={busy} disabled={!utterance.trim()} onClick={run}>Test</Button>
        </div>

        {error && <Callout tone="critical" title="Test failed">{error}</Callout>}

        {result && (
          <div className="col gap-12">
            <div className="row gap-8 wrap">
              {result.matchedIntent
                ? <span className="chip chip-good"><Icon name="check-circle" size={11} />Matched <code>{result.matchedIntent}</code></span>
                : <span className="chip chip-warning"><Icon name="alert" size={11} />No intent matched</span>}
              <div className="row gap-8" style={{ width: 160 }}>
                <Progress value={result.confidence * 100} tone={result.matchedIntent ? "good" : "warning"} />
                <span className="t-num t-micro">{(result.confidence * 100).toFixed(0)}%</span>
              </div>
              <span className="tag">route: {result.route}</span>
              {result.action && <span className="tag">action: {result.action}</span>}
              <span className="tag">fallback: {result.fallbackBehavior}</span>
            </div>

            <span className="t-sub" style={{ fontSize: 12.5 }}>{result.reason}</span>

            {(workflowName || apiName) && (
              <div className="row gap-8 wrap t-micro">
                {workflowName && <span className="row gap-4"><Icon name="workflow" size={13} />Workflow: <b>{workflowName}</b></span>}
                {apiName && <span className="row gap-4"><Icon name="zap" size={13} />API: <b>{apiName}</b></span>}
              </div>
            )}

            {result.entities.length > 0 && (
              <div className="table-wrap" style={{ border: "1px solid var(--hairline)", borderRadius: 10 }}>
                <table className="table">
                  <thead>
                    <tr><th>Entity</th><th>Matched</th><th>Value</th><th>Method</th></tr>
                  </thead>
                  <tbody>
                    {result.entities.map((en) => (
                      <tr key={en.name}>
                        <td><code style={{ fontSize: 12 }}>{en.name}</code></td>
                        <td>{en.matched ? <StatusChip status="good" label="matched" /> : <StatusChip status="neutral" label="missing" />}</td>
                        <td>
                          <span className="row gap-4">
                            {en.sensitive && <Icon name="lock" size={11} style={{ color: "var(--ink-3)" }} />}
                            <code style={{ fontSize: 12 }}>{en.maskedValue ?? en.value ?? "—"}</code>
                          </span>
                        </td>
                        <td><span className="tag" style={{ fontSize: 10.5 }}>{en.method}</span></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

/* ============================================================
   Intent create / edit drawer
   ============================================================ */

function IntentFormDrawer({ botId, intent, entities, workflows, apis, languages, canManage, permTitle, onClose, onSaved }: {
  botId: string;
  intent: Intent | null;
  entities: EntityDef[];
  workflows: Workflow[];
  apis: ApiConnection[];
  languages: LanguageOption[];
  canManage: boolean;
  permTitle?: string;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { toast } = useApp();
  const [name, setName] = useState(intent?.name ?? "");
  const [code, setCode] = useState(intent?.code ?? (intent ? suggestCode(intent.name) : ""));
  const [codeTouched, setCodeTouched] = useState(!!intent);
  const [category, setCategory] = useState(intent?.category ?? "");
  const [description, setDescription] = useState(intent?.description ?? "");
  const [samples, setSamples] = useState<string[]>(intent?.samples ?? []);
  const [sampleInput, setSampleInput] = useState("");
  const [sampleError, setSampleError] = useState<string | null>(null);
  const [langs, setLangs] = useState<string[]>(intent?.languages ?? []);
  const [threshold, setThreshold] = useState(intent?.confidenceThreshold ?? 0.65);
  const [required, setRequired] = useState<string[]>(intent?.entities ?? []);
  const [optional, setOptional] = useState<string[]>(intent?.optionalEntities ?? []);
  const [workflowId, setWorkflowId] = useState(intent?.workflowId ?? "");
  const [apiConnectionId, setApiConnectionId] = useState(intent?.apiConnectionId ?? "");
  const [priority, setPriority] = useState(intent?.priority ?? 0);
  const [fallbackBehavior, setFallbackBehavior] = useState(intent?.fallbackBehavior ?? "clarify");
  const [handoffEnabled, setHandoffEnabled] = useState(intent?.handoffEnabled ?? false);
  const [status, setStatus] = useState<Intent["status"]>(intent?.status ?? "active");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const entityOptions = entities.map((e) => ({ value: e.name, label: e.name }));

  const onNameChange = (v: string) => {
    setName(v);
    if (!codeTouched) setCode(suggestCode(v));
  };

  const addSample = () => {
    const v = sampleInput.trim();
    if (!v) return;
    if (samples.some((s) => normalizePhrase(s) === normalizePhrase(v))) {
      setSampleError("That training phrase is already in the list.");
      return;
    }
    setSamples((s) => [...s, v]);
    setSampleInput("");
    setSampleError(null);
  };

  const toggleRequired = (n: string) => {
    setRequired((r) => (r.includes(n) ? r.filter((x) => x !== n) : [...r, n]));
    setOptional((o) => o.filter((x) => x !== n));
  };
  const toggleOptional = (n: string) => {
    setOptional((o) => (o.includes(n) ? o.filter((x) => x !== n) : [...o, n]));
    setRequired((r) => r.filter((x) => x !== n));
  };

  const save = async () => {
    if (!name.trim()) { setError("Name is required."); return; }
    setBusy(true);
    setError(null);
    const body: Partial<Intent> = {
      name: name.trim(),
      code: code.trim() || undefined,
      category: category.trim() || undefined,
      description: description.trim(),
      samples,
      languages: langs,
      confidenceThreshold: threshold,
      entities: required,
      optionalEntities: optional,
      workflowId: workflowId || null,
      apiConnectionId: apiConnectionId || null,
      priority,
      fallbackBehavior,
      handoffEnabled,
      status,
    };
    try {
      if (intent) {
        await updateIntent(intent.id, body);
        toast("Intent updated");
      } else {
        await createIntent(botId, { ...body, name: name.trim() });
        toast("Intent created");
      }
      onSaved();
      onClose();
    } catch (e) {
      setError(errMsg(e, "Could not save intent"));
    } finally {
      setBusy(false);
    }
  };

  const below = intent ? intent.avgConfidence30d < intent.confidenceThreshold : false;

  return (
    <Drawer
      open onClose={onClose} wide
      title={intent
        ? <span className="row gap-8"><code>{intent.name}</code><StatusChip status={intent.status} /></span>
        : "New intent"}
      sub={intent ? `v${intent.version} · routes to ${intent.route}` : "Define what callers can ask for and where the conversation goes next."}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button variant="primary" icon="check" busy={busy} disabled={!canManage} title={permTitle} onClick={save}>
            {intent ? "Save changes" : "Create intent"}
          </Button>
        </>
      }
    >
      <div className="col gap-16">
        {error && <Callout tone="critical" title="Could not save">{error}</Callout>}

        {intent && below && (
          <Callout tone="critical" title="Below confidence threshold">
            Average confidence {(intent.avgConfidence30d * 100).toFixed(0)}% is under the {(intent.confidenceThreshold * 100).toFixed(0)}% threshold.
            Add more varied samples — especially phrasings from real escalated calls.
          </Callout>
        )}

        <div className="grid grid-2" style={{ gap: 12 }}>
          <Field label="Name" required>
            <input className="input" value={name} onChange={(e) => onNameChange(e.target.value)} placeholder="e.g. Book appointment" />
          </Field>
          <Field label="Code" hint="Stable identifier used in workflows and APIs.">
            <input className="input mono" value={code} onChange={(e) => { setCodeTouched(true); setCode(e.target.value); }} placeholder="book_appointment" />
          </Field>
          <Field label="Category">
            <input className="input" value={category} onChange={(e) => setCategory(e.target.value)} placeholder="e.g. Scheduling" />
          </Field>
          <Field label="Status">
            <select className="select" value={status} onChange={(e) => setStatus(e.target.value as Intent["status"])}>
              <option value="active">Active</option>
              <option value="needs_samples">Needs samples</option>
              <option value="disabled">Disabled</option>
            </select>
          </Field>
        </div>

        <Field label="Description">
          <textarea className="textarea" rows={2} value={description} onChange={(e) => setDescription(e.target.value)} placeholder="What this intent covers" />
        </Field>

        <div>
          <span className="t-label">Training phrases ({samples.length})</span>
          <div className="col gap-6 mt-8">
            {samples.map((s, i) => (
              <div key={`${s}-${i}`} className="row-between card-pad-sm" style={{ background: "var(--surface-2)", borderRadius: 8, fontSize: 12.5 }}>
                “{s}”
                <button className="btn-icon" style={{ width: 24, height: 24 }} aria-label={`Remove sample ${s}`} onClick={() => setSamples((arr) => arr.filter((_, j) => j !== i))}>
                  <Icon name="x" size={12} />
                </button>
              </div>
            ))}
            <div className="row gap-8">
              <input
                className="input grow" value={sampleInput} aria-label="Add training phrase" aria-invalid={sampleError ? true : undefined}
                placeholder="Add a training phrase, then press Enter…"
                onChange={(e) => { setSampleInput(e.target.value); if (sampleError) setSampleError(null); }}
                onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); addSample(); } }}
              />
              <Button size="sm" icon="plus" onClick={addSample}>Add</Button>
            </div>
            {sampleError && <span className="field-error"><Icon name="alert" size={12} />{sampleError}</span>}
          </div>
        </div>

        <div>
          <span className="t-label">Languages</span>
          <div className="mt-8">
            <LanguageChips options={languages} selected={langs} onToggle={(c) => setLangs((l) => (l.includes(c) ? l.filter((x) => x !== c) : [...l, c]))} />
          </div>
        </div>

        <div className="grid grid-2" style={{ gap: 12 }}>
          <Field label="Confidence threshold" hint="Below this, the fallback behavior runs.">
            <input className="input" type="number" step={0.01} min={0.3} max={0.95} value={threshold} onChange={(e) => setThreshold(Number(e.target.value))} />
          </Field>
          <Field label="Priority" hint="Higher wins when several intents tie.">
            <input className="input" type="number" step={1} value={priority} onChange={(e) => setPriority(Number(e.target.value))} />
          </Field>
        </div>

        <div className="grid grid-2" style={{ gap: 12 }}>
          <Field label="Required entities" hint="Must be extracted before routing.">
            <CheckList options={entityOptions} selected={required} onToggle={toggleRequired} emptyText="No entities defined yet" ariaLabel="Required entities" />
          </Field>
          <Field label="Optional entities">
            <CheckList options={entityOptions} selected={optional} onToggle={toggleOptional} emptyText="No entities defined yet" ariaLabel="Optional entities" />
          </Field>
        </div>

        <div className="grid grid-2" style={{ gap: 12 }}>
          <Field label="Associated workflow">
            <select className="select" value={workflowId ?? ""} onChange={(e) => setWorkflowId(e.target.value)}>
              <option value="">None</option>
              {workflows.map((w) => <option key={w.id} value={w.id}>{w.name}</option>)}
            </select>
          </Field>
          <Field label="Associated API">
            <select className="select" value={apiConnectionId ?? ""} onChange={(e) => setApiConnectionId(e.target.value)}>
              <option value="">None</option>
              {apis.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
            </select>
          </Field>
        </div>

        <div className="grid grid-2" style={{ gap: 12 }}>
          <Field label="Fallback behavior" hint="What happens below the confidence threshold.">
            <select className="select" value={fallbackBehavior} onChange={(e) => setFallbackBehavior(e.target.value)}>
              {FALLBACK_BEHAVIORS.map((f) => <option key={f.value} value={f.value}>{f.label}</option>)}
            </select>
          </Field>
          <div className="field">
            <span className="field-label">Human handoff</span>
            <div className="row gap-8" style={{ minHeight: 34 }}>
              <Toggle checked={handoffEnabled} onChange={setHandoffEnabled} label="Human handoff" />
              <span className="t-sub" style={{ fontSize: 12.5 }}>{handoffEnabled ? "Callers can reach a human from this intent" : "Off"}</span>
            </div>
          </div>
        </div>

        {intent && (
          <div className="row-between">
            <span className="t-label">Regression tests</span>
            <span className={`t-num t-strong ${intent.testPass < intent.testTotal ? "t-bad" : "t-good"}`}>{intent.testPass}/{intent.testTotal} passing</span>
          </div>
        )}
      </div>
    </Drawer>
  );
}

/* ============================================================
   Entity create / edit drawer (with live extraction test)
   ============================================================ */

function serializeSynonyms(syn?: Record<string, string[]>): string {
  if (!syn) return "";
  return Object.entries(syn).map(([k, v]) => `${k}: ${v.join(", ")}`).join("\n");
}

function parseSynonyms(text: string): Record<string, string[]> | undefined {
  const out: Record<string, string[]> = {};
  for (const line of text.split("\n")) {
    const t = line.trim();
    if (!t) continue;
    const idx = t.indexOf(":");
    const canonical = (idx === -1 ? t : t.slice(0, idx)).trim();
    if (!canonical) continue;
    out[canonical] = idx === -1 ? [] : t.slice(idx + 1).split(",").map((s) => s.trim()).filter(Boolean);
  }
  return Object.keys(out).length ? out : undefined;
}

function EntityFormDrawer({ entity, canManage, permTitle, onClose, onSaved }: {
  entity: EntityDef | null;
  canManage: boolean;
  permTitle?: string;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { toast } = useApp();
  const [name, setName] = useState(entity?.name ?? "");
  const [code, setCode] = useState(entity?.code ?? "");
  const [codeTouched, setCodeTouched] = useState(!!entity);
  const [description, setDescription] = useState(entity?.description ?? "");
  const [kind, setKind] = useState<EntityDef["kind"]>(entity?.kind ?? "custom");
  const [dataType, setDataType] = useState(entity?.dataType ?? "text");
  const [regexPattern, setRegexPattern] = useState(entity?.regexPattern ?? "");
  const [allowedValues, setAllowedValues] = useState<string[]>(entity?.allowedValues ?? []);
  const [synonymsText, setSynonymsText] = useState(serializeSynonyms(entity?.synonyms));
  const [example, setExample] = useState(entity?.example ?? "");
  const [pii, setPii] = useState(entity?.pii ?? false);
  const [maskingEnabled, setMaskingEnabled] = useState(entity?.maskingEnabled ?? false);
  const [requireConfirmation, setRequireConfirmation] = useState(entity?.requireConfirmation ?? false);
  const [retention, setRetention] = useState(entity?.retentionDays != null ? String(entity.retentionDays) : "");
  const [status, setStatus] = useState(entity?.status ?? "active");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  /* Extraction test (saved entities only) */
  const [testText, setTestText] = useState("");
  const [testBusy, setTestBusy] = useState(false);
  const [testError, setTestError] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<EntityExtraction | null>(null);

  const showRegex = kind === "regex" || dataType === "regex";
  const regexError = useMemo(() => {
    if (!showRegex || !regexPattern.trim()) return null;
    try {
      new RegExp(regexPattern);
      return null;
    } catch (e) {
      return errMsg(e, "Invalid regular expression");
    }
  }, [showRegex, regexPattern]);

  const onNameChange = (v: string) => {
    setName(v);
    if (!codeTouched) setCode(suggestCode(v));
  };

  const save = async () => {
    if (!name.trim()) { setError("Name is required."); return; }
    if (regexError) { setError("Fix the regex pattern before saving."); return; }
    setBusy(true);
    setError(null);
    const body: Partial<EntityDef> = {
      name: name.trim(),
      code: code.trim() || undefined,
      description: description.trim() || undefined,
      kind,
      dataType,
      synonyms: parseSynonyms(synonymsText),
      allowedValues: allowedValues.length ? allowedValues : undefined,
      regexPattern: showRegex && regexPattern.trim() ? regexPattern.trim() : undefined,
      maskingEnabled,
      requireConfirmation,
      retentionDays: retention.trim() === "" ? null : Number(retention),
      example: example.trim(),
      pii,
      status,
    };
    try {
      if (entity) {
        await updateEntity(entity.id, body);
        toast("Entity updated");
      } else {
        await createEntity({ ...body, name: name.trim() });
        toast("Entity created");
      }
      onSaved();
      onClose();
    } catch (e) {
      setError(errMsg(e, "Could not save entity"));
    } finally {
      setBusy(false);
    }
  };

  const runTest = async () => {
    if (!entity || !testText.trim() || testBusy) return;
    setTestBusy(true);
    setTestError(null);
    try {
      setTestResult(await testEntity(entity.id, testText.trim()));
    } catch (e) {
      setTestResult(null);
      setTestError(errMsg(e, "Extraction test failed"));
    } finally {
      setTestBusy(false);
    }
  };

  return (
    <Drawer
      open onClose={onClose} wide
      title={entity
        ? <span className="row gap-8"><code>{entity.name}</code>{entity.pii && <StatusChip status="warning" label="PII" />}</span>
        : "New entity"}
      sub={entity ? (entity.description || "Edit extraction rules and privacy handling.") : "Define a structured value the bot can extract from utterances."}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button variant="primary" icon="check" busy={busy} disabled={!canManage} title={permTitle} onClick={save}>
            {entity ? "Save changes" : "Create entity"}
          </Button>
        </>
      }
    >
      <div className="col gap-16">
        {error && <Callout tone="critical" title="Could not save">{error}</Callout>}

        <div className="grid grid-2" style={{ gap: 12 }}>
          <Field label="Name" required hint="Credential-like names (CVV, PIN, OTP, password) are rejected.">
            <input className="input" value={name} onChange={(e) => onNameChange(e.target.value)} placeholder="e.g. appointment_date" />
          </Field>
          <Field label="Code">
            <input className="input mono" value={code} onChange={(e) => { setCodeTouched(true); setCode(e.target.value); }} placeholder="appointment_date" />
          </Field>
        </div>

        <Field label="Description">
          <textarea className="textarea" rows={2} value={description} onChange={(e) => setDescription(e.target.value)} placeholder="What this entity captures" />
        </Field>

        <div className="grid grid-2" style={{ gap: 12 }}>
          <Field label="Kind">
            <select className="select" value={kind} onChange={(e) => setKind(e.target.value as EntityDef["kind"])}>
              <option value="system">System</option>
              <option value="custom">Custom</option>
              <option value="regex">Regex</option>
              <option value="api">API</option>
            </select>
          </Field>
          <Field label="Data type">
            <select className="select" value={dataType} onChange={(e) => setDataType(e.target.value)}>
              {DATA_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
            </select>
          </Field>
        </div>

        {showRegex && (
          <Field label="Regex pattern" error={regexError ?? undefined} hint="Applied against the transcript to extract the value.">
            <input
              className="input mono" value={regexPattern} aria-invalid={regexError ? true : undefined}
              onChange={(e) => setRegexPattern(e.target.value)} placeholder="e.g. \b\d{4}-\d{2}-\d{2}\b"
            />
          </Field>
        )}

        <Field label="Allowed values" hint="Press Enter to add. Leave empty to accept any value.">
          <ChipsInput values={allowedValues} onChange={setAllowedValues} placeholder="Add an allowed value…" ariaLabel="Add allowed value" />
        </Field>

        <Field label="Synonyms" hint={'One canonical value per line: "canonical: synonym1, synonym2"'}>
          <textarea
            className="textarea mono" rows={3} value={synonymsText}
            onChange={(e) => setSynonymsText(e.target.value)}
            placeholder={"tomorrow: tmrw, next day\nchecking: checking account, chequing"}
          />
        </Field>

        <Field label="Example" hint="Shown in the entities table to explain the extraction rule.">
          <input className="input" value={example} onChange={(e) => setExample(e.target.value)} placeholder='e.g. "next Thursday at 3pm" → 2026-07-23T15:00' />
        </Field>

        <div className="grid grid-2" style={{ gap: 12 }}>
          <div className="field">
            <span className="field-label">PII</span>
            <div className="row gap-8" style={{ minHeight: 34 }}>
              <Toggle checked={pii} onChange={setPii} label="PII" />
              <span className="t-sub" style={{ fontSize: 12.5 }}>{pii ? "Redacted from transcripts and logs" : "Not personal data"}</span>
            </div>
          </div>
          <div className="field">
            <span className="field-label">Masking</span>
            <div className="row gap-8" style={{ minHeight: 34 }}>
              <Toggle checked={maskingEnabled} onChange={setMaskingEnabled} label="Masking" />
              <span className="t-sub" style={{ fontSize: 12.5 }}>{maskingEnabled ? "Values are masked in tool calls and UI" : "Off"}</span>
            </div>
          </div>
          <div className="field">
            <span className="field-label">Require confirmation</span>
            <div className="row gap-8" style={{ minHeight: 34 }}>
              <Toggle checked={requireConfirmation} onChange={setRequireConfirmation} label="Require confirmation" />
              <span className="t-sub" style={{ fontSize: 12.5 }}>{requireConfirmation ? "Bot repeats the value back before use" : "Off"}</span>
            </div>
          </div>
          <Field label="Retention (days)" hint="Empty = tenant default retention.">
            <input className="input" type="number" min={0} value={retention} onChange={(e) => setRetention(e.target.value)} placeholder="e.g. 30" />
          </Field>
        </div>

        <Field label="Status">
          <select className="select" value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="active">Active</option>
            <option value="disabled">Disabled</option>
          </select>
        </Field>

        {entity && (
          <div className="card-pad-sm" style={{ border: "1px solid var(--hairline)", borderRadius: 10 }}>
            <span className="t-label">Try extraction</span>
            <div className="row gap-8 mt-8">
              <input
                className="input grow" value={testText} aria-label="Extraction test text"
                placeholder="e.g. my policy number is POL-2291-88"
                onChange={(e) => setTestText(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && runTest()}
              />
              <Button variant="primary" size="sm" icon="play" busy={testBusy} disabled={!testText.trim()} onClick={runTest}>Try extraction</Button>
            </div>
            {testError && <div className="mt-8"><Callout tone="critical">{testError}</Callout></div>}
            {testResult && (
              <div className="col gap-6 mt-8" style={{ fontSize: 12.5 }}>
                <div className="row gap-8 wrap">
                  {testResult.matched
                    ? <StatusChip status="good" label="Matched" />
                    : <StatusChip status="neutral" label="No match" />}
                  <span className="tag" style={{ fontSize: 10.5 }}>{testResult.method}</span>
                </div>
                {testResult.matched && (
                  <>
                    <span className="row gap-6">Value: <code>{testResult.sensitive ? (testResult.maskedValue ?? "•••") : (testResult.value ?? "—")}</code></span>
                    {testResult.maskedValue && <span className="row gap-6">Masked: <code>{testResult.maskedValue}</code></span>}
                    {testResult.sensitive && (
                      <span className="t-micro row gap-4">
                        <Icon name="lock" size={11} />
                        Shown masked because this entity is sensitive — the raw value is only used in-call.
                      </span>
                    )}
                  </>
                )}
              </div>
            )}
          </div>
        )}
        {!entity && <span className="t-micro">Save the entity first to try extraction against sample text.</span>}
      </div>
    </Drawer>
  );
}
