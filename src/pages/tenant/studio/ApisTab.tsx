import { useState } from "react";
import type { ApiConnection, VoiceBot } from "@/types/domain";
import { useAsync } from "@/hooks/useAsync";
import { listApis, testApiConnection } from "@/services/api";
import { Button, Callout, Drawer, StatusChip } from "@/components/ui";
import { DataTable } from "@/components/DataTable";
import { Icon } from "@/components/Icon";
import { useApp } from "@/state/AppContext";

const methodColor: Record<string, string> = { GET: "info", POST: "good", PUT: "warning", PATCH: "warning", DELETE: "critical" };

export default function ApisTab({ bot }: { bot: VoiceBot }) {
  const q = useAsync(() => listApis(bot.id), [bot.id]);
  const { toast } = useApp();
  const [open, setOpen] = useState<ApiConnection | null>(null);

  return (
    <div className="col gap-16">
      <div className="row-between">
        <span className="t-sub">Endpoints this bot may call mid-conversation. Secrets are stored as masked references — raw values never reach the browser.</span>
        <Button variant="primary" size="sm" icon="plus" onClick={() => toast("New connection scaffold created — configure and test before use", "info")}>New connection</Button>
      </div>

      <div className="card">
        <DataTable
          loading={q.loading} error={q.error} onRetry={q.reload} rows={q.data}
          onRowClick={(a) => setOpen(a)}
          empty={{ icon: "zap", title: "No API connections", body: "Connect scheduling, CRM or notification endpoints the bot can call during conversations." }}
          columns={[
            {
              key: "name", header: "Connection", sortValue: (a) => a.name,
              render: (a) => (
                <div className="row gap-10">
                  <span className={`chip chip-${methodColor[a.method]}`} style={{ fontFamily: "var(--mono)", fontSize: 10.5 }}>{a.method}</span>
                  <div><div className="t-strong">{a.name}</div><div className="t-micro mono truncate" style={{ maxWidth: 300 }}>{a.url}</div></div>
                </div>
              ),
            },
            { key: "auth", header: "Auth", render: (a) => <span className="tag">{a.authType === "api_key" ? "API key" : a.authType === "oauth2" ? "OAuth 2.0" : a.authType}</span> },
            { key: "status", header: "Health", sortValue: (a) => a.status, render: (a) => <StatusChip status={a.status} /> },
            { key: "latency", header: "p50", align: "right", sortValue: (a) => a.lastLatencyMs ?? 0, render: (a) => <span className="t-num">{a.lastLatencyMs ? `${a.lastLatencyMs}ms` : "—"}</span> },
            { key: "timeout", header: "Timeout / retries", align: "right", render: (a) => <span className="t-num t-sub">{a.timeoutMs / 1000}s · {a.retries}×</span> },
            { key: "version", header: "Ver", align: "right", render: (a) => <code>v{a.version}</code> },
          ]}
        />
      </div>

      <ApiDrawer conn={open} onClose={() => setOpen(null)} onTested={q.reload} />
    </div>
  );
}

function ApiDrawer({ conn, onClose, onTested }: { conn: ApiConnection | null; onClose: () => void; onTested?: () => void }) {
  const { toast } = useApp();
  const [testing, setTesting] = useState(false);
  const [result, setResult] = useState<Awaited<ReturnType<typeof testApiConnection>> | null>(null);

  if (!conn) return null;

  const runTest = async () => {
    setTesting(true);
    setResult(null);
    try {
      const r = await testApiConnection(conn.id);
      setResult(r);
      onTested?.();
    } catch (e) {
      toast(e instanceof Error ? e.message : "Connection test failed", "error");
    } finally {
      setTesting(false);
    }
  };

  return (
    <Drawer
      open onClose={onClose} wide
      title={<span className="row gap-8">{conn.name}<StatusChip status={conn.status} /></span>}
      sub={<span className="mono" style={{ fontSize: 12 }}>{conn.method} {conn.url}</span>}
      footer={
        <>
          <Button variant="ghost" icon="history" onClick={() => toast(`v${conn.version} current — previous versions can be restored as a new draft`, "info")}>Versions</Button>
          <Button variant="primary" icon="check" onClick={() => { toast("Connection saved to draft"); onClose(); }}>Save changes</Button>
        </>
      }
    >
      <div className="col gap-16">
        {conn.status === "failing" && (
          <Callout tone="critical" title={conn.lastTestedAt ? `Failing — last tested ${new Date(conn.lastTestedAt).toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}` : "Failing"}>
            All calls time out after {conn.timeoutMs / 1000}s. Escalations on this bot are elevated. Check the upstream service or raise the timeout.
          </Callout>
        )}

        <div className="grid grid-2" style={{ gap: 12 }}>
          <label className="field">
            <span className="field-label">Timeout (ms)</span>
            <input className="input" type="number" defaultValue={conn.timeoutMs} min={500} step={500} />
          </label>
          <label className="field">
            <span className="field-label">Retries</span>
            <input className="input" type="number" defaultValue={conn.retries} min={0} max={5} />
            <span className="field-hint">Exponential backoff between attempts.</span>
          </label>
        </div>

        <div className="col gap-6">
          <span className="field-label">Authentication</span>
          <div className="row gap-8 card-pad-sm" style={{ background: "var(--surface-2)", borderRadius: 10 }}>
            <Icon name="key" size={14} style={{ color: "var(--ink-3)" }} />
            <span className="tag">{conn.authType === "oauth2" ? "OAuth 2.0" : conn.authType === "api_key" ? "API key" : conn.authType}</span>
            <code className="t-micro grow truncate">{conn.secretRef}</code>
            <span className="chip chip-neutral"><Icon name="lock" size={11} />masked</span>
          </div>
          <span className="field-hint">Rotate secrets from Integrations → Secrets. The reference stays stable across rotations.</span>
        </div>

        <div className="col gap-6">
          <span className="field-label">Response mapping</span>
          <div className="col gap-6">
            {conn.responseMapping.map((m, i) => (
              <div key={i} className="row gap-8" style={{ fontSize: 12.5 }}>
                <code className="card-pad-sm grow" style={{ background: "var(--surface-2)", borderRadius: 8, padding: "6px 10px" }}>{m.from}</code>
                <Icon name="arrow-right" size={13} style={{ color: "var(--ink-3)" }} />
                <code className="card-pad-sm" style={{ background: "var(--brand-50)", color: "var(--brand-700)", borderRadius: 8, padding: "6px 10px" }}>{`{${m.to}}`}</code>
              </div>
            ))}
          </div>
          <span className="field-hint">Mapped variables become available in prompts and workflow conditions.</span>
        </div>

        {/* Test console */}
        <div className="card-pad-sm" style={{ border: "1px solid var(--hairline)", borderRadius: 10 }}>
          <div className="row-between">
            <span className="t-label">Test console</span>
            <Button size="sm" variant="primary" icon="play" busy={testing} onClick={runTest}>Send test request</Button>
          </div>
          {result && (
            <div className="mt-8 col gap-8">
              <div className="row gap-8">
                <StatusChip status={result.ok ? "healthy" : "failing"} label={`HTTP ${result.status}`} />
                <span className="t-micro t-num">{result.latencyMs}ms</span>
              </div>
              <pre className="card-pad-sm" style={{ background: "var(--sidebar-bg)", color: "#d6d2e6", borderRadius: 10, fontSize: 11.5, overflowX: "auto", margin: 0, padding: 12 }}>
                {JSON.stringify(JSON.parse(result.body), null, 2)}
              </pre>
              {!result.ok && <span className="t-micro" style={{ color: "var(--status-critical)" }}>Timeout after {conn.timeoutMs}ms — retried {conn.retries}×. The workflow’s failure branch will run when this happens in a call.</span>}
            </div>
          )}
        </div>
      </div>
    </Drawer>
  );
}
