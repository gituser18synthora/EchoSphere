import { useCallback, useEffect, useRef, useState } from "react";
import { getPlatformHealth, listAlertsPage, updateAlert, getAlert } from "@/services/api";
import { Button, CardSkeleton, Health, StatusChip, Tabs, EmptyState, ErrorState, Field, Modal } from "@/components/ui";
import { Sparkline } from "@/components/charts";
import { useApp } from "@/state/AppContext";
import type { HealthMetric, PlatformAlert } from "@/types/domain";
import { Paging } from "./security/Paging";

const POLL_MS = 30_000;
const STALE_MS = 90_000;
const tabs = [
  { id: "platform", label: "Platform Health" },
  { id: "ai", label: "AI Health" },
  { id: "telephony", label: "Telephony Health" },
  { id: "alerts", label: "Alerts" },
];
type Filters = { status?: PlatformAlert["status"]; severity?: PlatformAlert["severity"]; search?: string; page: number; pageSize: number };
const initialFilters: Filters = { page: 1, pageSize: 25 };
const errorMessage = (error: unknown) => error instanceof Error ? error.message : "The request failed. Please try again.";
const alertLevel = (value: string): HealthMetric["status"] => ["good", "warning", "serious", "critical", "neutral"].includes(value) ? value as HealthMetric["status"] : "neutral";
const time = (value: string | number) => new Date(value).toLocaleString();

/** Keep a successful snapshot while refreshing; an older request cannot replace a newer one. */
function useMonitoringQuery<T>(load: () => Promise<T>, dependencies: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [checkedAt, setCheckedAt] = useState<number | null>(null);
  const loader = useRef(load); loader.current = load;
  const generation = useRef(0);
  const reload = useCallback(async () => {
    const request = ++generation.current;
    setLoading(true);
    try {
      const result = await loader.current();
      if (request !== generation.current) return false;
      setData(result); setError(null); setCheckedAt(Date.now());
      return true;
    } catch (cause) {
      if (request === generation.current) setError(errorMessage(cause));
      return false;
    } finally {
      if (request === generation.current) setLoading(false);
    }
  }, []);
  useEffect(() => {
    void reload();
    return () => { generation.current += 1; };
    // The caller declares the inputs for this query; reload reads the latest loader.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reload, ...dependencies]);
  return { data, loading, error, checkedAt, reload };
}

function AlertDetails({ id, onClose, onChanged }: { id: string; onClose: () => void; onChanged: () => Promise<boolean> }) {
  const query = useMonitoringQuery(() => getAlert(id), [id]);
  const [note, setNote] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { toast, user } = useApp();
  const alert = query.data;
  const save = async (status: PlatformAlert["status"]) => {
    if (!alert) return;
    setSaving(true); setError(null);
    try {
      await updateAlert(id, status, { expectedStatus: alert.status, note: note.trim() || undefined });
      setNote("");
      toast(`Alert ${status === "open" ? "reopened" : status}.`);
      await Promise.all([query.reload(), onChanged()]);
    } catch (cause) {
      setError(errorMessage(cause));
      if ((cause as { status?: number }).status === 409) await Promise.all([query.reload(), onChanged()]);
    } finally { setSaving(false); }
  };
  return <Modal open title="Alert details" sub={alert?.title} wide onClose={() => { if (!saving) onClose(); }} footer={<Button disabled={saving} onClick={onClose}>Close</Button>}>
    {query.loading && !alert && <CardSkeleton rows={4} />}
    {query.error && <ErrorState message={query.error} onRetry={() => void query.reload()} />}
    {alert && <div className="col gap-12">
      <div className="row gap-12 wrap"><Health level={alertLevel(alert.severity)} label={String(alert.severity) === "info" ? "Info" : undefined} /><StatusChip status={alert.status} /><span className="t-sub">{alert.source} · {alert.scope}</span></div>
      <dl><dt>Occurred</dt><dd>{time(alert.time)}</dd>
        {alert.acknowledgedAt && <><dt>Acknowledged</dt><dd>{time(alert.acknowledgedAt)} · {alert.acknowledgedByName ?? alert.acknowledgedBy ?? "Recorded actor"}</dd></>}
        {alert.resolvedAt && <><dt>Resolved</dt><dd>{time(alert.resolvedAt)} · {alert.resolvedByName ?? alert.resolvedBy ?? "System"}</dd></>}
      </dl>
      {user?.role === "super_admin" && <div className="col gap-8">
        <Field label="Action note" hint="Optional context saved with the next status change."><textarea className="input" aria-label="Action note" rows={3} maxLength={2000} value={note} disabled={saving} onChange={(event) => setNote(event.target.value)} /></Field>
        <div className="row gap-8 wrap">
          {alert.status === "open" && <Button busy={saving} disabled={saving || query.loading || !!query.error} onClick={() => void save("acknowledged")}>Acknowledge alert</Button>}
          {alert.status !== "resolved" && <Button disabled={saving || query.loading || !!query.error} onClick={() => void save("resolved")}>Resolve alert</Button>}
          {alert.status === "resolved" && <Button busy={saving} disabled={saving || query.loading || !!query.error} onClick={() => void save("open")}>Reopen alert</Button>}
        </div>
      </div>}
      {error && <p role="alert" className="field-error">{error}</p>}
      <strong>Recorded history</strong>
      {alert.history.length ? <ol className="col gap-12">{alert.history.map((entry) => <li key={entry.id} className="card card-pad col gap-8">
        <div className="row-between gap-12 wrap"><strong>{entry.action}</strong><time>{time(entry.time)}</time></div>
        <span className="t-sub">{entry.actor}</span>{entry.note && <p>{entry.note}</p>}
        <details><summary>Recorded changes</summary><pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{JSON.stringify({ before: entry.previousValue, after: entry.newValue }, null, 2)}</pre></details>
      </li>)}</ol> : <p className="t-sub">No recorded changes yet.</p>}
      {alert.historyTruncated && <p className="t-sub">Showing the most recent 200 changes of {alert.historyCount} recorded events.</p>}
    </div>}
  </Modal>;
}

function RecordedHistory({ metric, now }: { metric: HealthMetric; now: number }) {
  const points = metric.history ?? [];
  if (!points.length) return <span className="t-micro">No recorded history yet. Waiting for collected health checks.</span>;
  const collectedAt = metric.collectedAt ?? points[points.length - 1].time;
  const collectorStale = now - Date.parse(collectedAt) > STALE_MS;
  const end = Date.parse(metric.checkedAt ?? points[points.length - 1].time);
  const start = end - (metric.historyWindowHours ?? 24) * 3_600_000;
  const color = (status: HealthMetric["status"]) => `var(--${status === "neutral" ? "status-neutral" : `viz-${status}`})`;
  return <div className="col gap-8">
    <span className="t-micro">Recorded availability · {metric.sampleCount ?? points.length} samples{metric.uptimePercent != null ? ` · ${metric.uptimePercent.toFixed(1)}% of ${metric.knownSampleCount == null ? "rated checks" : `${metric.knownSampleCount} rated check${metric.knownSampleCount === 1 ? "" : "s"}`} healthy${metric.unknownSampleCount == null ? "" : `; ${metric.unknownSampleCount} unrated`}` : ""}</span>
    <span className="t-micro">History last collected: {time(collectedAt)}</span>
    {collectorStale && <span className="field-error" role="status">History collection is stale. Live checks may be newer than the recorded history.</span>}
    <svg width="100%" height={18} viewBox="0 0 1000 18" preserveAspectRatio="none" role="img" aria-label={`${metric.name} recorded health history`}>
      {points.map((point, index) => <rect key={`${point.time}-${index}`} x={Math.min(995, Math.max(0, (Date.parse(point.time) - start) / (end - start) * 995))} y={0} width={5} height={18} rx={1} fill={color(point.status)}>
        <title>{`${time(point.time)} · ${point.status}${point.latencyMs != null ? ` · ${point.latencyMs} ms` : ""}`}</title>
      </rect>)}
    </svg>
    <div className="row-between t-micro"><span>{time(start)}</span><span>{time(end)}</span></div>
    <details className="t-micro"><summary>View recorded checks</summary>
      <p>Marks show recorded checks. Gaps contain no observations. Historical summaries retain the worst state in each time bucket.</p>
      <div style={{ maxHeight: 240, overflow: "auto" }}><table style={{ width: "100%", textAlign: "left" }}><thead><tr><th>Time</th><th>Health</th><th>Response</th></tr></thead>
        <tbody>{points.map((point, index) => <tr key={`${point.time}-${index}`}><td>{time(point.time)}</td><td>{point.status}</td><td>{point.latencyMs == null ? "—" : `${point.latencyMs} ms`}</td></tr>)}</tbody>
      </table></div>
    </details>
  </div>;
}

function CheckTime({ label, checkedAt, now, failed }: { label: string; checkedAt: number | null; now: number; failed: boolean }) {
  const stale = checkedAt !== null && now - checkedAt > STALE_MS;
  return <span className="row gap-8 wrap"><span>{label}: {checkedAt ? time(checkedAt) : "No successful check yet"}</span>
    {(stale || failed) && <StatusChip status="stale" label={failed ? "Refresh failed" : "Data is stale"} />}</span>;
}

export default function Monitoring() {
  const [tab, setTab] = useState("platform");
  const [filters, setFilters] = useState<Filters>({ ...initialFilters });
  const [search, setSearch] = useState("");
  const health = useMonitoringQuery(getPlatformHealth);
  const alerts = useMonitoringQuery(() => listAlertsPage(filters), [filters]);
  const { toast, user } = useApp();
  const [refreshing, setRefreshing] = useState(false);
  const [pending, setPending] = useState<Record<string, boolean>>({});
  const [actionErrors, setActionErrors] = useState<Record<string, string>>({});
  const [selectedAlert, setSelectedAlert] = useState<string | null>(null);
  const [now, setNow] = useState(Date.now());
  const canManageAlerts = user?.role === "super_admin";

  useEffect(() => {
    const timer = window.setTimeout(() => setFilters((current) => {
      const next = search.trim() || undefined;
      return current.search === next ? current : { ...current, search: next, page: 1 };
    }), 300);
    return () => window.clearTimeout(timer);
  }, [search]);

  useEffect(() => {
    const refreshVisible = () => {
      setNow(Date.now());
      if (document.visibilityState === "visible") {
        void health.reload(); void alerts.reload();
      }
    };
    const timer = window.setInterval(refreshVisible, POLL_MS);
    document.addEventListener("visibilitychange", refreshVisible);
    return () => { window.clearInterval(timer); document.removeEventListener("visibilitychange", refreshVisible); };
  }, [health.reload, alerts.reload]);

  // The final alert on a page can disappear after a status change or automatic recovery.
  useEffect(() => {
    const totalPages = alerts.data?.meta.totalPages;
    if (!alerts.loading && !alerts.error && totalPages && filters.page > totalPages) {
      setFilters((current) => ({ ...current, page: totalPages }));
    }
  }, [alerts.data, alerts.loading, alerts.error, filters.page]);

  const refresh = async () => {
    setRefreshing(true);
    const results = await Promise.all([health.reload(), alerts.reload()]);
    setNow(Date.now()); setRefreshing(false);
    toast(results.every(Boolean) ? "Monitoring refreshed." : "Monitoring refresh failed. Check the errors below.", results.every(Boolean) ? "good" : "error");
  };
  const changeStatus = async (alert: PlatformAlert, status: PlatformAlert["status"]) => {
    setPending((current) => ({ ...current, [alert.id]: true }));
    setActionErrors((current) => ({ ...current, [alert.id]: "" }));
    try {
      await updateAlert(alert.id, status, { expectedStatus: alert.status });
      toast(`Alert ${status === "open" ? "reopened" : status}.`);
      await alerts.reload();
    } catch (cause) {
      const message = errorMessage(cause);
      setActionErrors((current) => ({ ...current, [alert.id]: message }));
      toast(message, "error");
      if ((cause as { status?: number }).status === 409) await alerts.reload();
    } finally {
      setPending((current) => ({ ...current, [alert.id]: false }));
    }
  };

  const renderMetrics = (metrics: HealthMetric[]) => <div className="col gap-12">
    {health.error && <ErrorState message={`Health checks failed: ${health.error}`} onRetry={() => void health.reload()} />}
    {health.error && health.data && <p role="status" className="t-sub">Showing the last successful health check. Current service health is unconfirmed.</p>}
    <div className="grid grid-3" aria-busy={health.loading}>
      {health.loading && !health.data && Array.from({ length: 3 }).map((_, i) => <CardSkeleton key={i} rows={2} />)}
      {!health.loading && !health.error && metrics.length === 0 && <EmptyState icon="activity" title="No services in this group" body="Nothing is registered for this tab yet." />}
      {metrics.map((metric) => {
        const trend = metric.spark;
        return <div key={metric.key ?? metric.name} className="card card-pad col gap-8">
        <div className="row-between"><span className="t-strong" style={{ fontSize: 13.5 }}>{metric.name}</span><Health level={metric.status} /></div>
        <span className="kpi-value t-num" style={{ fontSize: 21 }}>{metric.value}</span>
        <div className="row-between"><span className="t-micro">{metric.target}</span>
          {trend.length > 1 && <span aria-label="Recorded response time trend"><Sparkline data={trend} width={110} height={26}
            color={metric.status === "critical" ? "var(--viz-critical)" : metric.status === "warning" ? "var(--viz-warning)" : "var(--series-2)"} /></span>}
        </div>
        {metric.detail && <span className="t-micro" style={{ overflowWrap: "anywhere" }}>{metric.detail}</span>}
        {metric.checkedAt && <span className="t-micro">Checked: {time(metric.checkedAt)}</span>}
        {metric.historyError && <span className="field-error" role="status">History unavailable: {metric.historyError}</span>}
        {metric.observations && Object.keys(metric.observations).length > 0 && <dl className="t-micro">{Object.entries(metric.observations).filter(([, value]) => value != null).map(([key, value]) => <div className="row-between gap-12" key={key}>
          <dt>{key.replace(/([A-Z])/g, " $1").replace(/^./, (letter) => letter.toUpperCase())}</dt><dd>{typeof value === "object" ? JSON.stringify(value) : String(value)}</dd>
        </div>)}</dl>}
        <RecordedHistory metric={metric} now={now} />
      </div>; })}
    </div>
  </div>;

  return <>
    <div className="page-head"><div className="page-head-titles"><h1 className="page-title">Monitoring</h1>
      <p className="page-sub">Platform, AI and telephony health · Recorded history over the last 24 hours</p></div>
      <div className="page-actions"><Button icon="refresh" busy={refreshing} disabled={health.loading || alerts.loading} onClick={() => void refresh()}>Refresh</Button></div>
    </div>
    <div className="card card-pad col gap-8" style={{ marginBottom: 16 }}>
      <span className="t-sub">Automatically refreshes every 30 seconds while this page is visible. History contains recorded checks only.</span>
      <div className="row gap-16 wrap t-micro" aria-live="polite">
        <CheckTime label="Health last checked" checkedAt={health.checkedAt} now={now} failed={!!health.error} />
        <CheckTime label="Alerts last checked" checkedAt={alerts.checkedAt} now={now} failed={!!alerts.error} />
      </div>
    </div>
    <Tabs tabs={tabs.map((item) => item.id === "alerts" && alerts.data?.meta.counts ? { ...item, count: alerts.data.meta.counts.open + alerts.data.meta.counts.acknowledged } : item)} active={tab} onChange={setTab} />
    <div className="mt-16">
      {tab !== "alerts" && renderMetrics((health.data ?? []).filter((metric) => metric.group === tab))}
      {tab === "alerts" && <div className="col gap-12">
        <div className="card card-pad col gap-12"><div className="row gap-12 wrap">
          <Field label="Search alerts"><input className="input" placeholder="Title or source" value={search} onChange={(event) => setSearch(event.target.value)} /></Field>
          <Field label="Status"><select className="select" value={filters.status ?? ""} onChange={(event) => setFilters((current) => ({ ...current, status: (event.target.value || undefined) as Filters["status"], page: 1 }))}>
            <option value="">All statuses</option><option value="open">Open</option><option value="acknowledged">Acknowledged</option><option value="resolved">Resolved</option></select></Field>
          <Field label="Severity"><select className="select" value={filters.severity ?? ""} onChange={(event) => setFilters((current) => ({ ...current, severity: (event.target.value || undefined) as Filters["severity"], page: 1 }))}>
            <option value="">All severities</option><option value="critical">Critical</option><option value="serious">At risk</option><option value="warning">Degraded</option><option value="good">Healthy</option><option value="neutral">No data</option><option value="info">Info</option></select></Field>
        </div><div><Button variant="ghost" onClick={() => { setSearch(""); setFilters({ ...initialFilters }); }}>Clear filters</Button></div>
          <p className="t-micro">Acknowledgements, resolutions and action notes are saved in alert history. Recovered services resolve incidents automatically. A new failure after recovery creates a new incident.</p>
        </div>
        <div className="card">
          {alerts.error ? <ErrorState message={`Alerts could not be loaded: ${alerts.error}`} onRetry={() => void alerts.reload()} />
            : alerts.loading ? <div style={{ padding: 16 }}><CardSkeleton rows={4} /></div>
            : !alerts.data?.items.length ? <EmptyState icon="activity" title="No matching alerts" body="No recorded alerts match these filters. Check the health tabs for current service status." />
            : <div className="col" style={{ padding: 16, gap: 10 }}>{alerts.data.items.map((alert) => <div key={alert.id} className="col gap-8 card-pad-sm" style={{ border: "1px solid var(--hairline)", borderRadius: 10 }}>
              <div className="row gap-12 wrap" style={{ alignItems: "flex-start" }}>
                <span className={`health-dot ${alertLevel(alert.severity)}`} style={{ marginTop: 5 }} />
                <div className="grow"><div style={{ fontSize: 13, fontWeight: 600 }}>{alert.title}</div>
                  <div className="t-micro mt-4">{alert.source} · {time(alert.time)} · scope: {alert.scope}</div>
                </div><Health level={alertLevel(alert.severity)} label={String(alert.severity) === "info" ? "Info" : undefined} /><StatusChip status={alert.status} />
                <Button size="sm" variant="ghost" onClick={() => setSelectedAlert(alert.id)}>View details</Button>
                {canManageAlerts && <div className="row gap-8 wrap" aria-label={`Actions for ${alert.title}`}>
                  {alert.status === "open" && <Button size="sm" busy={pending[alert.id]} onClick={() => void changeStatus(alert, "acknowledged")}>Acknowledge</Button>}
                  {alert.status !== "resolved" && <Button size="sm" disabled={pending[alert.id]} onClick={() => void changeStatus(alert, "resolved")}>Resolve</Button>}
                  {alert.status === "resolved" && <Button size="sm" busy={pending[alert.id]} onClick={() => void changeStatus(alert, "open")}>Reopen</Button>}
                </div>}
              </div>
              {actionErrors[alert.id] && <p className="field-error" role="alert">{actionErrors[alert.id]}</p>}
            </div>)}</div>}
        </div>
        <Paging page={filters.page} size={filters.pageSize} total={alerts.data?.meta.total ?? 0} pages={alerts.data?.meta.totalPages ?? 1} loading={alerts.loading || !!alerts.error}
          onPage={(page) => setFilters((current) => ({ ...current, page }))} onSize={(pageSize) => setFilters((current) => ({ ...current, pageSize, page: 1 }))} />
      </div>}
    </div>
    {selectedAlert && <AlertDetails id={selectedAlert} onClose={() => setSelectedAlert(null)} onChanged={alerts.reload} />}
  </>;
}
