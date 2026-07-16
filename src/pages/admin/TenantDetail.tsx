import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useAsync } from "@/hooks/useAsync";
import {
  getTenant, getTenantAnalytics, listAudit, listBots, listKnowledge,
  listReleases, listSubscriptions, listTeam,
} from "@/services/api";
import {
  Button, CardSkeleton, EmptyState, ErrorState, Health, KpiCard, StatusChip,
  Tabs, Timeline, Avatar,
} from "@/components/ui";
import { DataTable, type Column } from "@/components/DataTable";
import { fmtNum, ChartCard, LineChart, Legend } from "@/components/charts";
import { Icon } from "@/components/Icon";
import type { Release, Tenant, VoiceBot } from "@/types/domain";
import { useApp } from "@/state/AppContext";

const tabs = [
  { id: "overview", label: "Overview" },
  { id: "users", label: "Users" },
  { id: "bots", label: "Bots" },
  { id: "knowledge", label: "Knowledge" },
  { id: "usage", label: "Usage & Billing" },
  { id: "ai", label: "AI Usage" },
  { id: "deployments", label: "Deployments" },
  { id: "audit", label: "Audit Log" },
];

export default function TenantDetail() {
  const { tenantId } = useParams();
  const navigate = useNavigate();
  const { toast } = useApp();
  const [tab, setTab] = useState("overview");
  const tenantQ = useAsync(() => getTenant(tenantId!), [tenantId]);

  if (tenantQ.error) return <ErrorState message={tenantQ.error} onRetry={tenantQ.reload} />;
  if (tenantQ.loading) return <div className="grid grid-2"><CardSkeleton rows={6} /><CardSkeleton rows={6} /></div>;
  const t = tenantQ.data;
  if (!t) {
    return <EmptyState icon="building" title="Tenant not found" body="It may have been removed or the link is stale." action={<Button onClick={() => navigate("/admin/tenants")}>Back to Organizations</Button>} />;
  }

  return (
    <>
      <div className="page-head">
        <div className="row gap-16">
          <div className="icon-tile brand" style={{ width: 46, height: 46 }}><Icon name="building" size={21} /></div>
          <div className="page-head-titles">
            <h1 className="page-title">
              {t.name}
              <StatusChip status={t.status} />
            </h1>
            <p className="page-sub">{t.domain} · {t.industry} · {t.region} · customer since {new Date(t.createdAt).toLocaleDateString("en-US", { month: "short", year: "numeric" })}</p>
          </div>
        </div>
        <div className="page-actions">
          <Button icon="mail" onClick={() => toast(`Invite sent to ${t.adminEmail}`, "info")}>Contact admin</Button>
          <Button variant="primary" icon="external" onClick={() => toast("Impersonation requires a second approver (four-eyes policy).", "info")}>
            Impersonate
          </Button>
        </div>
      </div>

      <Tabs tabs={tabs} active={tab} onChange={setTab} />

      <div className="mt-16">
        {tab === "overview" && <OverviewTab tenant={t} />}
        {tab === "users" && <UsersTab tenantId={t.id} />}
        {tab === "bots" && <BotsTab tenantId={t.id} />}
        {tab === "knowledge" && <KnowledgeTab tenantId={t.id} />}
        {tab === "usage" && <UsageTab tenantId={t.id} />}
        {tab === "ai" && <AiUsageTab tenantId={t.id} />}
        {tab === "deployments" && <DeploymentsTab tenantId={t.id} />}
        {tab === "audit" && <AuditTab tenantName={t.name} />}
      </div>
    </>
  );
}

function OverviewTab({ tenant: t }: { tenant: Tenant }) {
  const a = useAsync(() => getTenantAnalytics(30, undefined, t.id), [t.id]);
  return (
    <>
      <div className="grid grid-5">
        <KpiCard label="Health" value={t.health === "good" ? "Healthy" : t.health === "warning" ? "Degraded" : t.health === "serious" ? "At risk" : t.health === "critical" ? "Critical" : "No data"} icon="activity" />
        <KpiCard label="Active users" value={String(t.users)} icon="users" />
        <KpiCard label="VoiceBots" value={String(t.bots)} icon="bot" />
        <KpiCard label="Calls this month" value={fmtNum(t.callsMonth)} icon="phone" />
        <KpiCard label="MRR" value={`$${fmtNum(t.mrr)}`} icon="dollar" />
      </div>
      <div className="grid grid-2 mt-16">
        <ChartCard title="Call volume" sub="Last 30 days" legend={<Legend shape="line" items={[{ label: "Calls", color: "var(--series-1)" }]} />}>
          {a.loading ? <CardSkeleton rows={5} /> : a.error ? <ErrorState message={a.error} onRetry={a.reload} /> : (
            <LineChart data={a.data!.callsSeries} x="t" series={[{ key: "calls", label: "Calls", area: true }]} height={200} />
          )}
        </ChartCard>
        <div className="card">
          <div className="card-header"><span className="card-title">Account summary</span><Health level={t.health} /></div>
          <div className="col" style={{ padding: 18, gap: 12 }}>
            {[
              ["Plan", <span key="p" className="tag" style={{ textTransform: "capitalize" }}>{t.plan}</span>],
              ["Primary admin", t.adminEmail],
              ["Minutes this month", `${fmtNum(t.minutesMonth)} min`],
              ["AI cost this month", `$${fmtNum(t.aiCostMonth)}`],
              ["Data region", t.region],
              ["Tenant ID", <code key="id">{t.id}</code>],
            ].map(([k, v], i) => (
              <div className="row-between" key={i} style={{ borderBottom: i < 5 ? "1px solid var(--hairline)" : "none", paddingBottom: i < 5 ? 10 : 0 }}>
                <span className="t-sub">{k}</span>
                <span className="t-body t-strong t-num">{v}</span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </>
  );
}

function UsersTab({ tenantId }: { tenantId: string }) {
  const q = useAsync(() => listTeam(tenantId), [tenantId]);
  return (
    <div className="card">
      <DataTable
        loading={q.loading}
        error={q.error}
        onRetry={q.reload}
        rows={q.data}
        empty={{ icon: "users", title: "No users provisioned", body: "Users appear here once the tenant admin completes onboarding." }}
        columns={[
          { key: "name", header: "User", sortValue: (m) => m.name, render: (m) => <div className="row gap-12"><Avatar name={m.name} /><div><div className="t-strong">{m.name}</div><div className="t-micro">{m.email}</div></div></div> },
          { key: "role", header: "Role", sortValue: (m) => m.role, render: (m) => <span className="tag">{m.role}</span> },
          { key: "status", header: "Status", render: (m) => <StatusChip status={m.status} /> },
          { key: "last", header: "Last active", render: (m) => <span className="t-sub">{m.lastActive === "—" ? "—" : new Date(m.lastActive).toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}</span> },
        ]}
      />
    </div>
  );
}

function BotsTab({ tenantId }: { tenantId: string }) {
  const q = useAsync(() => listBots(tenantId), [tenantId]);
  const cols: Column<VoiceBot>[] = [
    { key: "name", header: "Bot", sortValue: (b) => b.name, render: (b) => <div><div className="t-strong">{b.name}</div><div className="t-micro">{b.useCase}</div></div> },
    { key: "status", header: "Status", render: (b) => <StatusChip status={b.status} /> },
    { key: "version", header: "Live version", render: (b) => <code>{b.liveVersion ?? "—"}</code> },
    { key: "health", header: "Health", render: (b) => <Health level={b.health} /> },
    { key: "calls", header: "Calls / mo", align: "right", sortValue: (b) => b.callsMonth, render: (b) => <span className="t-num">{fmtNum(b.callsMonth)}</span> },
    { key: "containment", header: "Containment", align: "right", render: (b) => <span className="t-num">{b.containment ? `${b.containment}%` : "—"}</span> },
  ];
  return (
    <div className="card">
      <DataTable loading={q.loading} error={q.error} onRetry={q.reload} rows={q.data} columns={cols}
        empty={{ icon: "bot", title: "No bots yet", body: "This tenant hasn't created any VoiceBots." }} />
    </div>
  );
}

function KnowledgeTab({ tenantId }: { tenantId: string }) {
  const q = useAsync(() => listKnowledge(undefined, tenantId), [tenantId]);
  return (
    <div className="card">
      <DataTable loading={q.loading} error={q.error} onRetry={q.reload} rows={q.data}
        empty={{ icon: "book", title: "No knowledge sources", body: "Documents, URLs and FAQs the tenant indexes will appear here." }}
        columns={[
          { key: "name", header: "Source", sortValue: (k) => k.name, render: (k) => <div><div className="t-strong">{k.name}</div><div className="t-micro">{k.detail}</div></div> },
          { key: "type", header: "Type", render: (k) => <span className="tag" style={{ textTransform: "capitalize" }}>{k.type}</span> },
          { key: "status", header: "Index status", render: (k) => <StatusChip status={k.status} /> },
          { key: "chunks", header: "Chunks", align: "right", sortValue: (k) => k.chunks, render: (k) => <span className="t-num">{fmtNum(k.chunks)}</span> },
          { key: "quality", header: "Quality", align: "right", sortValue: (k) => k.quality, render: (k) => <span className="t-num">{k.quality ? `${k.quality}%` : "—"}</span> },
        ]}
      />
    </div>
  );
}

function UsageTab({ tenantId }: { tenantId: string }) {
  const a = useAsync(() => getTenantAnalytics(30, undefined, tenantId), [tenantId]);
  const subsQ = useAsync(listSubscriptions, []);
  const sub = subsQ.data?.find((s) => s.tenantId === tenantId);
  const minutesSeries = (a.data?.callsSeries ?? []).map((p) => ({ t: p.t, min: Math.round((p.calls as number) * 3.2) }));
  return (
    <div className="grid grid-2">
      <ChartCard title="Minutes consumed" sub="Daily voice minutes, last 30 days" legend={<Legend shape="line" items={[{ label: "Minutes", color: "var(--series-1)" }]} />}>
        {a.loading ? <CardSkeleton rows={5} /> : (
          <LineChart data={minutesSeries} x="t" series={[{ key: "min", label: "Minutes", area: true }]} height={200} />
        )}
      </ChartCard>
      <div className="card">
        <div className="card-header"><span className="card-title">Billing snapshot</span>{sub && <StatusChip status={sub.status} />}</div>
        <div className="col" style={{ padding: 18, gap: 12 }}>
          {subsQ.loading ? <CardSkeleton rows={4} /> : !sub ? (
            <EmptyState icon="card" title="No subscription" body="This tenant has no active subscription." />
          ) : (
            [
              ["Monthly recurring", `$${fmtNum(sub.mrr)}`],
              ["Plan", <span key="p" className="tag" style={{ textTransform: "capitalize" }}>{sub.plan}</span>],
              ["Minutes used / included", `${fmtNum(sub.minutesUsed)} / ${fmtNum(sub.minutesIncluded)}`],
              ["Seats", `${sub.seats}`],
              ["Renews", sub.renewsAt ? new Date(sub.renewsAt).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" }) : "—"],
            ].map(([k, v], i) => (
              <div className="row-between" key={i} style={{ borderBottom: i < 4 ? "1px solid var(--hairline)" : "none", paddingBottom: i < 4 ? 10 : 0 }}>
                <span className="t-sub">{k}</span><span className="t-strong t-num">{v}</span>
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
}

function AiUsageTab({ tenantId }: { tenantId: string }) {
  const a = useAsync(() => getTenantAnalytics(30, undefined, tenantId), [tenantId]);
  return (
    <div className="grid grid-2">
      <ChartCard
        title="AI cost by component"
        sub="Daily USD, last 30 days"
        legend={<Legend items={[
          { label: "LLM", color: "var(--series-1)" }, { label: "TTS", color: "var(--series-2)" },
          { label: "STT", color: "var(--series-3)" }, { label: "Telephony", color: "var(--series-4)" },
        ]} />}
      >
        {a.loading ? <CardSkeleton rows={5} /> : a.error ? <ErrorState message={a.error} onRetry={a.reload} /> : (
          <LineChart
            data={a.data!.costSeries}
            x="t"
            yFmt={(v) => `$${fmtNum(v)}`}
            series={[
              { key: "llm", label: "LLM" }, { key: "tts", label: "TTS" },
              { key: "stt", label: "STT" }, { key: "telephony", label: "Telephony" },
            ]}
            height={220}
          />
        )}
      </ChartCard>
      <div className="card card-pad col gap-12">
        <span className="card-title">Governance notes</span>
        <p className="t-sub">Model routing, embedding configuration and token budgets for this tenant are managed centrally under <b>AI Governance</b>. Tenant admins see only approved voices and capabilities — never provider credentials or raw model settings.</p>
        <div className="callout callout-info">
          <Icon name="info" size={15} />
          <div className="callout-body">This tenant uses the platform-default conversation model with the standard PII redaction and medical-advice guardrails enabled.</div>
        </div>
      </div>
    </div>
  );
}

function DeploymentsTab({ tenantId }: { tenantId: string }) {
  const q = useAsync(async () => {
    const bots = await listBots(tenantId);
    const perBot = await Promise.all(
      bots.slice(0, 6).map(async (b) => {
        const rels = await listReleases(b.id);
        return rels.map((r) => ({ release: r, botName: b.name }));
      }),
    );
    return perBot.flat().sort((x, y) => (y.release.publishedAt ?? "").localeCompare(x.release.publishedAt ?? ""));
  }, [tenantId]);

  if (q.loading) return <div className="card card-pad"><CardSkeleton rows={4} /></div>;
  if (q.error) return <ErrorState message={q.error} onRetry={q.reload} />;
  if (!q.data || q.data.length === 0) {
    return <div className="card"><EmptyState icon="rocket" title="No deployments" body="Publish history for this tenant's bots will appear here." /></div>;
  }
  return (
    <div className="card card-pad">
      <Timeline
        items={q.data.map(({ release: r, botName }: { release: Release; botName: string }) => ({
          icon: r.stage === "published" ? "rocket" : r.stage === "rolled_back" ? "undo" : "clock",
          tone: r.stage === "published" ? "good" : r.stage === "rolled_back" ? "critical" : "brand",
          title: <>{botName} <code>{r.version}</code> — {r.stage.replace("_", " ")}</>,
          meta: `${r.requestedBy}${r.approvedBy ? ` · approved by ${r.approvedBy}` : ""}${r.publishedAt ? ` · ${new Date(r.publishedAt).toLocaleDateString("en-US", { month: "short", day: "numeric" })}` : ""}`,
          body: r.notes,
        }))}
      />
    </div>
  );
}

function AuditTab({ tenantName }: { tenantName: string }) {
  const q = useAsync(listAudit, []);
  const rows = (q.data ?? []).filter((a) => a.tenant === tenantName);
  return (
    <div className="card">
      <DataTable loading={q.loading} error={q.error} onRetry={q.reload} rows={rows}
        empty={{ icon: "shield", title: "No audit events", body: "Actions affecting this tenant will be recorded here." }}
        columns={[
          { key: "time", header: "Time", sortValue: (a) => a.time, render: (a) => <span className="t-sub t-num">{new Date(a.time).toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}</span> },
          { key: "actor", header: "Actor", render: (a) => <div className="row gap-6"><Avatar name={a.actor} /><span>{a.actor}</span></div> },
          { key: "action", header: "Action", render: (a) => <span className="t-sub">{a.action}</span> },
          { key: "target", header: "Target", render: (a) => <code style={{ fontSize: 12 }}>{a.target}</code> },
          { key: "ip", header: "IP", render: (a) => <span className="t-micro t-num">{a.ip}</span> },
        ]}
      />
    </div>
  );
}
