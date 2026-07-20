import { useMemo, useState } from "react";
import type { ApiConnection, ApiTestResult, Intent, VoiceBot, Workflow } from "@/types/domain";
import { useAsync } from "@/hooks/useAsync";
import {
  createApi, deleteApi, duplicateApi, listApis, listIntents, listWorkflows,
  testApiConnection, updateApi,
} from "@/services/api";
import {
  Button, Callout, ConfirmModal, Drawer, Field, MenuButton, StatusChip, Toggle,
} from "@/components/ui";
import { DataTable } from "@/components/DataTable";
import { Icon } from "@/components/Icon";
import { useApp } from "@/state/AppContext";

const methodColor: Record<string, string> = { GET: "info", POST: "good", PUT: "warning", PATCH: "warning", DELETE: "critical" };
const METHODS: ApiConnection["method"][] = ["GET", "POST", "PUT", "PATCH", "DELETE"];
const BODY_METHODS = new Set(["POST", "PUT", "PATCH"]);
const AUTH_TYPES: { value: ApiConnection["authType"]; label: string }[] = [
  { value: "none", label: "None" },
  { value: "api_key", label: "API key" },
  { value: "bearer", label: "Bearer token" },
  { value: "basic", label: "Basic" },
  { value: "oauth2", label: "OAuth 2.0" },
];
const BUILTIN_VARIABLES = "{{tenant_id}} {{bot_id}} {{call_id}} {{session_id}} {{user_id}} {{customer_phone}} {{intent.code}} {{entities.<name>}}";
const VAR_RE = /\{\{\s*([a-zA-Z0-9_.-]+)\s*\}\}/g;

const errMsg = (e: unknown, fallback: string) => (e instanceof Error ? e.message : fallback);

const authLabel = (t: ApiConnection["authType"]) => AUTH_TYPES.find((a) => a.value === t)?.label ?? t;

type KV = { k: string; v: string };
const recordToRows = (rec?: Record<string, string>): KV[] => Object.entries(rec ?? {}).map(([k, v]) => ({ k, v: String(v) }));
const rowsToRecord = (rows: KV[]): Record<string, string> => {
  const out: Record<string, string> = {};
  for (const { k, v } of rows) if (k.trim()) out[k.trim()] = v;
  return out;
};

function detectEntityVars(...sources: string[]): string[] {
  const found = new Set<string>();
  for (const src of sources) {
    for (const m of src.matchAll(VAR_RE)) {
      if (m[1].startsWith("entities.")) found.add(m[1]);
    }
  }
  return [...found];
}

function prettyBody(body: string): string {
  try {
    return JSON.stringify(JSON.parse(body), null, 2);
  } catch {
    return body;
  }
}

/* ---------- small building blocks ---------- */

function KVEditor({ rows, onChange, label, keyPlaceholder = "Key", valuePlaceholder = "Value" }: {
  rows: KV[]; onChange: (rows: KV[]) => void; label: string; keyPlaceholder?: string; valuePlaceholder?: string;
}) {
  return (
    <div className="col gap-6">
      {rows.map((row, i) => (
        <div key={i} className="row gap-8">
          <input
            className="input mono" style={{ flex: 1 }} value={row.k} placeholder={keyPlaceholder} aria-label={`${label} key ${i + 1}`}
            onChange={(e) => onChange(rows.map((r, j) => (j === i ? { ...r, k: e.target.value } : r)))}
          />
          <input
            className="input mono" style={{ flex: 1.5 }} value={row.v} placeholder={valuePlaceholder} aria-label={`${label} value ${i + 1}`}
            onChange={(e) => onChange(rows.map((r, j) => (j === i ? { ...r, v: e.target.value } : r)))}
          />
          <button className="btn-icon" aria-label={`Remove ${label} row ${i + 1}`} onClick={() => onChange(rows.filter((_, j) => j !== i))}>
            <Icon name="x" size={13} />
          </button>
        </div>
      ))}
      <div>
        <Button size="sm" icon="plus" onClick={() => onChange([...rows, { k: "", v: "" }])}>Add {label.toLowerCase()}</Button>
      </div>
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

function SectionTitle({ children }: { children: string }) {
  return <span className="t-label" style={{ display: "block", borderBottom: "1px solid var(--hairline)", paddingBottom: 6 }}>{children}</span>;
}

/* ============================================================ */

export default function ApisTab({ bot }: { bot: VoiceBot }) {
  const q = useAsync(() => listApis(bot.id), [bot.id]);
  const intentsQ = useAsync(() => listIntents(bot.id), [bot.id]);
  const workflowsQ = useAsync(listWorkflows, []);
  const { toast, hasPermission } = useApp();

  const canManage = hasPermission("manage_api_connections") || hasPermission("integrations.manage");
  const canTest = hasPermission("test_api_connections") || canManage;
  const managePermTitle = canManage ? undefined : "Requires the manage_api_connections permission";
  const testPermTitle = canTest ? undefined : "Requires the test_api_connections permission";

  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<ApiConnection | null>(null);
  const [archiving, setArchiving] = useState<ApiConnection | null>(null);
  const [archiveBusy, setArchiveBusy] = useState(false);
  const [archiveError, setArchiveError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const rowAction = async (fn: () => Promise<unknown>, success: string) => {
    setActionError(null);
    try {
      await fn();
      toast(success);
      q.reload();
    } catch (e) {
      setActionError(errMsg(e, "Action failed"));
    }
  };

  const confirmArchive = async () => {
    if (!archiving) return;
    setArchiveBusy(true);
    setArchiveError(null);
    try {
      await deleteApi(archiving.id);
      toast(`Connection “${archiving.name}” archived`);
      setArchiving(null);
      q.reload();
    } catch (e) {
      setArchiveError(errMsg(e, "Could not archive connection"));
    } finally {
      setArchiveBusy(false);
    }
  };

  return (
    <div className="col gap-16">
      <div className="row-between">
        <span className="t-sub">Endpoints this bot may call mid-conversation. Secrets are stored as masked references — raw values never reach the browser.</span>
        <Button variant="primary" size="sm" icon="plus" disabled={!canManage} title={managePermTitle} onClick={() => setCreating(true)}>
          New connection
        </Button>
      </div>

      {actionError && <Callout tone="critical" title="Action failed">{actionError}</Callout>}

      <div className="card">
        <DataTable
          loading={q.loading} error={q.error} onRetry={q.reload} rows={q.data}
          onRowClick={(a) => setEditing(a)}
          empty={{ icon: "zap", title: "No API connections", body: "Connect scheduling, CRM or notification endpoints the bot can call during conversations." }}
          columns={[
            {
              key: "name", header: "Connection", sortValue: (a) => a.name,
              render: (a) => (
                <div className="row gap-10">
                  <span className={`chip chip-${methodColor[a.method]}`} style={{ fontFamily: "var(--mono)", fontSize: 10.5 }}>{a.method}</span>
                  <div><div className="t-strong">{a.name}</div><div className="t-micro mono truncate" style={{ maxWidth: 260 }}>{a.url}</div></div>
                </div>
              ),
            },
            { key: "auth", header: "Auth", sortValue: (a) => a.authType, render: (a) => <span className="tag">{authLabel(a.authType)}</span> },
            {
              key: "state", header: "State-changing", sortValue: (a) => (a.isStateChanging ? 1 : 0),
              render: (a) => a.isStateChanging
                ? <span className="chip chip-warning"><Icon name="alert" size={11} />Yes{a.requireConfirmation ? " · confirm" : ""}</span>
                : <span className="t-micro">—</span>,
            },
            {
              key: "status", header: "Last test", sortValue: (a) => a.status,
              render: (a) => (
                <div className="col gap-2">
                  <StatusChip status={a.status} />
                  {a.lastTestedAt && <span className="t-micro">{new Date(a.lastTestedAt).toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}</span>}
                </div>
              ),
            },
            { key: "latency", header: "p50", align: "right", sortValue: (a) => a.lastLatencyMs ?? 0, render: (a) => <span className="t-num">{a.lastLatencyMs ? `${a.lastLatencyMs}ms` : "—"}</span> },
            { key: "timeout", header: "Timeout / retries", align: "right", render: (a) => <span className="t-num t-sub">{a.timeoutMs / 1000}s · {a.retries}×</span> },
            {
              key: "actions", header: "", width: 120,
              render: (a) => (
                <div className="row gap-4" style={{ justifyContent: "flex-end" }}>
                  <Button
                    size="sm" icon="play" disabled={!canTest} title={canTest ? "Open the test console" : testPermTitle}
                    onClick={(e) => { e.stopPropagation(); setEditing(a); }}
                  >
                    Test
                  </Button>
                  <MenuButton
                    label={`Actions for ${a.name}`}
                    actions={[
                      { label: "Edit", icon: "edit", onClick: () => setEditing(a) },
                      { label: "Duplicate", icon: "copy", disabled: !canManage, onClick: () => rowAction(() => duplicateApi(a.id), `Connection “${a.name}” duplicated`) },
                      {
                        label: a.status === "disabled" ? "Activate" : "Deactivate",
                        icon: a.status === "disabled" ? "check-circle" : "pause",
                        disabled: !canManage,
                        onClick: () => rowAction(
                          () => updateApi(a.id, { status: a.status === "disabled" ? "untested" : "disabled" }),
                          a.status === "disabled" ? `Connection “${a.name}” activated — run a test before use` : `Connection “${a.name}” deactivated`,
                        ),
                      },
                      "sep",
                      { label: "Archive", icon: "trash", danger: true, disabled: !canManage, onClick: () => { setArchiveError(null); setArchiving(a); } },
                    ]}
                  />
                </div>
              ),
            },
          ]}
        />
      </div>

      {(creating || editing) && (
        <ApiBuilderDrawer
          key={editing?.id ?? "new-connection"}
          botId={bot.id}
          conn={editing}
          intents={intentsQ.data ?? []}
          workflows={workflowsQ.data ?? []}
          canManage={canManage}
          canTest={canTest}
          managePermTitle={managePermTitle}
          testPermTitle={testPermTitle}
          onClose={() => { setCreating(false); setEditing(null); }}
          onSaved={q.reload}
        />
      )}

      <ConfirmModal
        open={!!archiving}
        onClose={() => setArchiving(null)}
        onConfirm={confirmArchive}
        title="Archive connection?"
        confirmLabel="Archive" danger busy={archiveBusy}
        body={
          <div className="col gap-8">
            {archiveError && <Callout tone="critical">{archiveError}</Callout>}
            <span>
              <code>{archiving?.name}</code> will no longer be callable from conversations.
              Archiving is blocked while intents or workflows still reference it.
            </span>
          </div>
        }
      />
    </div>
  );
}

/* ============================================================
   Request builder drawer (create + edit) with test console
   ============================================================ */

function ApiBuilderDrawer({ botId, conn, intents, workflows, canManage, canTest, managePermTitle, testPermTitle, onClose, onSaved }: {
  botId: string;
  conn: ApiConnection | null;
  intents: Intent[];
  workflows: Workflow[];
  canManage: boolean;
  canTest: boolean;
  managePermTitle?: string;
  testPermTitle?: string;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { toast } = useApp();

  /* Basics */
  const [name, setName] = useState(conn?.name ?? "");
  const [description, setDescription] = useState(conn?.description ?? "");
  const [method, setMethod] = useState<ApiConnection["method"]>(conn?.method ?? "GET");
  const [url, setUrl] = useState(conn?.url ?? "");
  /* Auth */
  const [authType, setAuthType] = useState<ApiConnection["authType"]>(conn?.authType ?? "none");
  const [secretRef, setSecretRef] = useState(conn?.secretRef ?? "");
  /* Request */
  const [headers, setHeaders] = useState<KV[]>(recordToRows(conn?.headers));
  const [queryParams, setQueryParams] = useState<KV[]>(recordToRows(conn?.queryParams));
  const [pathParams, setPathParams] = useState<KV[]>(recordToRows(conn?.pathParams));
  const [bodyText, setBodyText] = useState(conn?.bodyTemplate ? JSON.stringify(conn.bodyTemplate, null, 2) : "");
  /* Response */
  const [successCondition, setSuccessCondition] = useState(conn?.successCondition ?? "");
  const [successMessage, setSuccessMessage] = useState(conn?.successMessage ?? "");
  const [failureMessage, setFailureMessage] = useState(conn?.failureMessage ?? "");
  const [sensitiveMasks, setSensitiveMasks] = useState<string[]>(conn?.sensitiveMasks ?? []);
  /* Associations */
  const [allowedIntents, setAllowedIntents] = useState<string[]>(conn?.allowedIntents ?? []);
  const [allowedWorkflows, setAllowedWorkflows] = useState<string[]>(conn?.allowedWorkflows ?? []);
  /* Behavior */
  const [timeoutMs, setTimeoutMs] = useState(conn?.timeoutMs ?? 5000);
  const [retries, setRetries] = useState(conn?.retries ?? 1);
  const [isStateChanging, setIsStateChanging] = useState(conn?.isStateChanging ?? false);
  const [requireConfirmation, setRequireConfirmation] = useState(conn?.requireConfirmation ?? false);

  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const showBody = BODY_METHODS.has(method);
  const bodyError = useMemo(() => {
    if (!showBody || !bodyText.trim()) return null;
    try {
      const parsed: unknown = JSON.parse(bodyText);
      if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return "Body template must be a JSON object.";
      return null;
    } catch (e) {
      return `Invalid JSON — ${errMsg(e, "cannot parse")}`;
    }
  }, [showBody, bodyText]);

  const secretError = authType !== "none" && secretRef.trim() !== "" && !secretRef.trim().startsWith("secret://")
    ? 'Secret references must start with "secret://".'
    : null;

  /* Test console (edit only) */
  const savedVars = useMemo(
    () => (conn
      ? detectEntityVars(
          conn.url,
          JSON.stringify(conn.headers ?? {}),
          JSON.stringify(conn.queryParams ?? {}),
          JSON.stringify(conn.pathParams ?? {}),
          conn.bodyTemplate ? JSON.stringify(conn.bodyTemplate) : "",
        )
      : []),
    [conn],
  );
  const [testValues, setTestValues] = useState<Record<string, string>>({});
  const [testing, setTesting] = useState(false);
  const [testError, setTestError] = useState<string | null>(null);
  const [result, setResult] = useState<ApiTestResult | null>(null);

  const runTest = async () => {
    if (!conn || testing) return;
    setTesting(true);
    setTestError(null);
    setResult(null);
    try {
      const filled = Object.fromEntries(Object.entries(testValues).filter(([, v]) => v.trim() !== ""));
      const r = await testApiConnection(conn.id, Object.keys(filled).length ? filled : undefined);
      setResult(r);
      toast(r.ok ? "Test request succeeded" : "Test request completed with a failure", r.ok ? "good" : "info");
      onSaved();
    } catch (e) {
      setTestError(errMsg(e, "Connection test failed"));
    } finally {
      setTesting(false);
    }
  };

  const save = async () => {
    if (!name.trim()) { setError("Name is required."); return; }
    if (!url.trim()) { setError("URL is required."); return; }
    if (authType !== "none" && !secretRef.trim()) { setError('Provide a secret reference (e.g. "secret://my-crm-key") or set auth to None.'); return; }
    if (secretError) { setError(secretError); return; }
    if (bodyError) { setError("Fix the body template JSON before saving."); return; }

    setBusy(true);
    setError(null);
    const payload: Partial<ApiConnection> = {
      name: name.trim(),
      description: description.trim() || undefined,
      method,
      url: url.trim(),
      authType,
      secretRef: authType === "none" ? undefined : secretRef.trim(),
      headers: rowsToRecord(headers),
      queryParams: rowsToRecord(queryParams),
      pathParams: rowsToRecord(pathParams),
      bodyTemplate: showBody && bodyText.trim() ? (JSON.parse(bodyText) as Record<string, unknown>) : null,
      successCondition: successCondition.trim() || undefined,
      successMessage: successMessage.trim() || undefined,
      failureMessage: failureMessage.trim() || undefined,
      sensitiveMasks,
      allowedIntents,
      allowedWorkflows,
      isStateChanging,
      requireConfirmation,
      timeoutMs: Number(timeoutMs),
      retries: Number(retries),
    };
    try {
      if (conn) {
        await updateApi(conn.id, payload);
        toast("Connection updated");
      } else {
        await createApi({ ...payload, name: name.trim(), url: url.trim(), botId });
        toast("Connection created — run a test before using it in calls");
      }
      onSaved();
      onClose();
    } catch (e) {
      setError(errMsg(e, "Could not save connection"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Drawer
      open onClose={onClose} wide
      title={conn
        ? <span className="row gap-8">{conn.name}<StatusChip status={conn.status} /></span>
        : "New API connection"}
      sub={conn
        ? <span className="mono" style={{ fontSize: 12 }}>{conn.method} {conn.url}</span>
        : "Build the request the bot sends mid-conversation."}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button variant="primary" icon="check" busy={busy} disabled={!canManage} title={managePermTitle} onClick={save}>
            {conn ? "Save changes" : "Create connection"}
          </Button>
        </>
      }
    >
      <div className="col gap-16">
        {error && <Callout tone="critical" title="Could not save">{error}</Callout>}

        {conn?.status === "failing" && (
          <Callout tone="critical" title={conn.lastTestedAt ? `Failing — last tested ${new Date(conn.lastTestedAt).toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}` : "Failing"}>
            Calls to this endpoint are failing. Escalations on this bot are elevated. Check the upstream service, then re-test below.
          </Callout>
        )}

        {/* Basics */}
        <SectionTitle>Basics</SectionTitle>
        <div className="grid grid-2" style={{ gap: 12 }}>
          <Field label="Name" required>
            <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. CRM — create ticket" />
          </Field>
          <Field label="Method">
            <select className="select" value={method} onChange={(e) => setMethod(e.target.value as ApiConnection["method"])}>
              {METHODS.map((m) => <option key={m} value={m}>{m}</option>)}
            </select>
          </Field>
        </div>
        <Field label="Description">
          <input className="input" value={description} onChange={(e) => setDescription(e.target.value)} placeholder="What this connection does" />
        </Field>
        <Field label="URL" required hint={`Available variables: ${BUILTIN_VARIABLES}`}>
          <input className="input mono" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://api.example.com/tickets/{{entities.ticket_id}}" />
        </Field>

        {/* Auth */}
        <SectionTitle>Auth</SectionTitle>
        <div className="grid grid-2" style={{ gap: 12 }}>
          <Field label="Auth type">
            <select className="select" value={authType} onChange={(e) => setAuthType(e.target.value as ApiConnection["authType"])}>
              {AUTH_TYPES.map((a) => <option key={a.value} value={a.value}>{a.label}</option>)}
            </select>
          </Field>
          {authType !== "none" && (
            <Field label="Secret reference" required error={secretError ?? undefined} hint="Reference only — the key itself comes from server environment.">
              <input
                className="input mono" value={secretRef} aria-invalid={secretError ? true : undefined}
                onChange={(e) => setSecretRef(e.target.value)} placeholder="secret://my-crm-key"
              />
            </Field>
          )}
        </div>

        {/* Request */}
        <SectionTitle>Headers</SectionTitle>
        <KVEditor rows={headers} onChange={setHeaders} label="Header" keyPlaceholder="Header name" valuePlaceholder="Value — {{variables}} allowed" />

        <SectionTitle>Query params</SectionTitle>
        <KVEditor rows={queryParams} onChange={setQueryParams} label="Query param" keyPlaceholder="Param" valuePlaceholder="Value — {{variables}} allowed" />

        <SectionTitle>Path params</SectionTitle>
        <KVEditor rows={pathParams} onChange={setPathParams} label="Path param" keyPlaceholder="Param" valuePlaceholder="Value — {{variables}} allowed" />

        {showBody && (
          <>
            <SectionTitle>Body</SectionTitle>
            <Field label="JSON body template" error={bodyError ?? undefined} hint={`Sent with ${method} requests. Variables: ${BUILTIN_VARIABLES}`}>
              <textarea
                className="textarea mono" rows={6} value={bodyText} aria-invalid={bodyError ? true : undefined}
                onChange={(e) => setBodyText(e.target.value)}
                placeholder={'{\n  "customer": "{{customer_phone}}",\n  "date": "{{entities.date}}"\n}'}
                style={{ fontSize: 12 }}
              />
            </Field>
          </>
        )}

        {/* Response */}
        <SectionTitle>Response</SectionTitle>
        <div className="grid grid-2" style={{ gap: 12 }}>
          <Field label="Success condition" hint="e.g. status < 400">
            <input className="input mono" value={successCondition} onChange={(e) => setSuccessCondition(e.target.value)} placeholder="status < 400" />
          </Field>
          <Field label="Sensitive masks" hint="Header / field names masked in logs and test output.">
            <ChipsInput values={sensitiveMasks} onChange={setSensitiveMasks} placeholder="e.g. Authorization — press Enter" ariaLabel="Add sensitive mask" />
          </Field>
          <Field label="Success message" hint="What the bot says when the call succeeds.">
            <input className="input" value={successMessage} onChange={(e) => setSuccessMessage(e.target.value)} placeholder="Done — your appointment is booked." />
          </Field>
          <Field label="Failure message" hint="What the bot says when the call fails.">
            <input className="input" value={failureMessage} onChange={(e) => setFailureMessage(e.target.value)} placeholder="I couldn't reach the booking system." />
          </Field>
        </div>

        {/* Associations */}
        <SectionTitle>Associations</SectionTitle>
        <div className="grid grid-2" style={{ gap: 12 }}>
          <Field label="Allowed intents" hint="Empty = callable from any intent.">
            <CheckList
              options={intents.map((i) => ({ value: i.id, label: i.name }))}
              selected={allowedIntents}
              onToggle={(id) => setAllowedIntents((s) => (s.includes(id) ? s.filter((x) => x !== id) : [...s, id]))}
              emptyText="No intents defined yet"
              ariaLabel="Allowed intents"
            />
          </Field>
          <Field label="Allowed workflows" hint="Empty = callable from any workflow.">
            <CheckList
              options={workflows.map((w) => ({ value: w.id, label: w.name }))}
              selected={allowedWorkflows}
              onToggle={(id) => setAllowedWorkflows((s) => (s.includes(id) ? s.filter((x) => x !== id) : [...s, id]))}
              emptyText="No workflows defined yet"
              ariaLabel="Allowed workflows"
            />
          </Field>
        </div>

        {/* Behavior */}
        <SectionTitle>Behavior</SectionTitle>
        <div className="grid grid-2" style={{ gap: 12 }}>
          <Field label="Timeout (ms)">
            <input className="input" type="number" min={500} step={500} value={timeoutMs} onChange={(e) => setTimeoutMs(Number(e.target.value))} />
          </Field>
          <Field label="Retries" hint="Exponential backoff between attempts.">
            <input className="input" type="number" min={0} max={5} value={retries} onChange={(e) => setRetries(Number(e.target.value))} />
          </Field>
          <div className="field">
            <span className="field-label">State-changing</span>
            <div className="row gap-8" style={{ minHeight: 34 }}>
              <Toggle checked={isStateChanging} onChange={setIsStateChanging} label="State-changing" />
              <span className="t-sub" style={{ fontSize: 12.5 }}>{isStateChanging ? "Creates or modifies data" : "Read-only"}</span>
            </div>
          </div>
          <div className="field">
            <span className="field-label">Require confirmation</span>
            <div className="row gap-8" style={{ minHeight: 34 }}>
              <Toggle checked={requireConfirmation} onChange={setRequireConfirmation} label="Require confirmation" />
              <span className="t-sub" style={{ fontSize: 12.5 }}>{requireConfirmation ? "Bot confirms with the caller first" : "Off"}</span>
            </div>
          </div>
        </div>
        {isStateChanging && !requireConfirmation && (
          <Callout tone="warning" title="Confirmation recommended">
            This connection changes data. Enabling confirmation makes the bot repeat the action back to the caller before executing it.
          </Callout>
        )}

        {/* Test console — saved connections only */}
        {conn && (
          <>
            <SectionTitle>Test console</SectionTitle>
            <div className="card-pad-sm col gap-10" style={{ border: "1px solid var(--hairline)", borderRadius: 10 }}>
              <div className="row-between">
                <span className="t-micro">Runs against the last saved configuration — save your edits first if you changed the request.</span>
                <Button size="sm" variant="primary" icon="play" busy={testing} disabled={!canTest} title={testPermTitle} onClick={runTest}>
                  Send test request
                </Button>
              </div>

              {savedVars.length > 0 && (
                <div className="col gap-6">
                  <span className="t-label">Test values for entity variables</span>
                  {savedVars.map((v) => (
                    <div key={v} className="row gap-8">
                      <code className="tag" style={{ minWidth: 160 }}>{`{{${v}}}`}</code>
                      <input
                        className="input grow" value={testValues[v] ?? ""} aria-label={`Test value for ${v}`}
                        placeholder="sample value"
                        onChange={(e) => setTestValues((t) => ({ ...t, [v]: e.target.value }))}
                      />
                    </div>
                  ))}
                </div>
              )}

              {testError && <Callout tone="critical" title="Test failed">{testError}</Callout>}

              {result && (
                <div className="col gap-8">
                  <div className="row gap-8 wrap">
                    <StatusChip status={result.ok ? "healthy" : "failing"} label={result.ok ? "OK" : "Failed"} />
                    <span className="tag t-num">HTTP {result.status}</span>
                    <span className="t-micro t-num">{result.latencyMs}ms</span>
                    {result.contentType && <span className="t-micro mono">{result.contentType}</span>}
                  </div>

                  {result.error && <Callout tone="critical" title="Error">{result.error}</Callout>}
                  {result.redirectedTo && (
                    <Callout tone="warning" title="Redirects are not followed">
                      The endpoint answered with a redirect to <code>{result.redirectedTo}</code>. Point the URL at the final location.
                    </Callout>
                  )}
                  {result.userMessage && <Callout tone="info" title="Caller-facing message">{result.userMessage}</Callout>}

                  {result.body && (
                    <>
                      <pre className="card-pad-sm" style={{ background: "var(--sidebar-bg)", color: "#d6d2e6", borderRadius: 10, fontSize: 11.5, overflow: "auto", maxHeight: 280, margin: 0, padding: 12 }}>
                        {prettyBody(result.body)}
                      </pre>
                      {result.truncated && <span className="t-micro row gap-4"><Icon name="info" size={11} />Response body truncated for display.</span>}
                    </>
                  )}

                  {result.headersSent && Object.keys(result.headersSent).length > 0 && (
                    <div className="col gap-4">
                      <span className="t-label">Headers sent (sensitive values masked)</span>
                      <div className="table-wrap" style={{ border: "1px solid var(--hairline)", borderRadius: 10 }}>
                        <table className="table">
                          <thead><tr><th>Header</th><th>Value</th></tr></thead>
                          <tbody>
                            {Object.entries(result.headersSent).map(([k, v]) => (
                              <tr key={k}>
                                <td><code style={{ fontSize: 11.5 }}>{k}</code></td>
                                <td><code style={{ fontSize: 11.5 }}>{v}</code></td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    </div>
                  )}
                </div>
              )}
            </div>
          </>
        )}
      </div>
    </Drawer>
  );
}
