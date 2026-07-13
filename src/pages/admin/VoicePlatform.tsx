import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAsync } from "@/hooks/useAsync";
import { listBots, listPhoneNumbers, listTenants } from "@/services/api";
import { DataTable } from "@/components/DataTable";
import { Button, Health, StatusChip, Tabs, Callout } from "@/components/ui";
import { fmtNum } from "@/components/charts";
import { Icon } from "@/components/Icon";
import { useApp } from "@/state/AppContext";

const tabs = [
  { id: "bots", label: "VoiceBots" },
  { id: "numbers", label: "Phone Numbers" },
  { id: "sip", label: "SIP & Telephony" },
  { id: "channels", label: "Channels" },
];

export default function VoicePlatform() {
  const [tab, setTab] = useState("bots");
  return (
    <>
      <div className="page-head">
        <div className="page-head-titles">
          <h1 className="page-title">Voice Platform</h1>
          <p className="page-sub">Every bot, number and trunk on the platform</p>
        </div>
      </div>
      <Tabs tabs={tabs} active={tab} onChange={setTab} />
      <div className="mt-16">
        {tab === "bots" && <AllBots />}
        {tab === "numbers" && <Numbers />}
        {tab === "sip" && <Sip />}
        {tab === "channels" && <ChannelsSummary />}
      </div>
    </>
  );
}

function AllBots() {
  const botsQ = useAsync(listBots, []);
  const tenantsQ = useAsync(listTenants, []);
  const navigate = useNavigate();
  const tenantName = (id: string) => tenantsQ.data?.find((t) => t.id === id)?.name ?? id;
  return (
    <div className="card">
      <DataTable
        loading={botsQ.loading} error={botsQ.error} onRetry={botsQ.reload} rows={botsQ.data}
        onRowClick={(b) => navigate(`/admin/tenants/${b.tenantId}`)}
        empty={{ icon: "bot", title: "No bots on the platform" }}
        columns={[
          { key: "name", header: "Bot", sortValue: (b) => b.name, render: (b) => <div><div className="t-strong">{b.name}</div><div className="t-micro">{tenantName(b.tenantId)}</div></div> },
          { key: "status", header: "Status", sortValue: (b) => b.status, render: (b) => <StatusChip status={b.status} /> },
          { key: "health", header: "Health", sortValue: (b) => b.health, render: (b) => <Health level={b.health} /> },
          { key: "version", header: "Live", render: (b) => <code>{b.liveVersion ?? "—"}</code> },
          { key: "langs", header: "Languages", render: (b) => <span className="t-sub">{b.languages.join(", ")}</span> },
          { key: "calls", header: "Calls / mo", align: "right", sortValue: (b) => b.callsMonth, render: (b) => <span className="t-num">{fmtNum(b.callsMonth)}</span> },
          { key: "cost", header: "Cost / call", align: "right", sortValue: (b) => b.avgCostPerCall, render: (b) => <span className="t-num">{b.avgCostPerCall ? `$${b.avgCostPerCall.toFixed(2)}` : "—"}</span> },
        ]}
      />
    </div>
  );
}

function Numbers() {
  const q = useAsync(listPhoneNumbers, []);
  const { toast } = useApp();
  return (
    <div className="card">
      <div className="card-header">
        <span className="card-title">Phone number inventory</span>
        <Button size="sm" variant="primary" icon="plus" onClick={() => toast("Number purchase flow requires carrier API credentials (TODO_BACKEND)", "info")}>Buy number</Button>
      </div>
      <DataTable
        loading={q.loading} error={q.error} onRetry={q.reload} rows={q.data}
        empty={{ icon: "phone", title: "No numbers provisioned" }}
        columns={[
          { key: "number", header: "Number", sortValue: (n) => n.number, render: (n) => <code className="t-strong" style={{ fontSize: 12.5 }}>{n.number}</code> },
          { key: "country", header: "Country" },
          { key: "tenant", header: "Tenant", render: (n) => n.tenant ?? <span className="t-micro">Unassigned</span> },
          { key: "bot", header: "Bot", render: (n) => n.bot ?? "—" },
          { key: "provider", header: "Carrier" },
          { key: "status", header: "Status", sortValue: (n) => n.status, render: (n) => <StatusChip status={n.status} /> },
          { key: "cost", header: "Monthly", align: "right", sortValue: (n) => n.monthlyCost, render: (n) => <span className="t-num">${n.monthlyCost.toFixed(2)}</span> },
        ]}
      />
    </div>
  );
}

function Sip() {
  const trunks = [
    { id: "trk-1", name: "Twilio elastic trunk — US", region: "US", channels: 240, usage: 61, status: "good" as const },
    { id: "trk-2", name: "Voxbone trunk 3 — EU-West", region: "EU", channels: 120, usage: 84, status: "critical" as const },
    { id: "trk-3", name: "Telnyx LATAM trunk", region: "LATAM", channels: 60, usage: 42, status: "good" as const },
  ];
  return (
    <>
      <Callout tone="critical" title="Trunk degradation in EU-West-2">
        Voxbone trunk 3 shows 8.2% call setup failures since 10:05 UTC. Failover to the backup trunk is armed; carrier ticket #88214 open.
      </Callout>
      <div className="grid grid-3 mt-16">
        {trunks.map((t) => (
          <div key={t.id} className="card card-pad col gap-12">
            <div className="row-between">
              <span className="t-strong" style={{ fontSize: 13.5 }}>{t.name}</span>
              <Health level={t.status} />
            </div>
            <div className="row gap-16">
              <div><div className="t-micro">Region</div><div className="t-strong">{t.region}</div></div>
              <div><div className="t-micro">Channels</div><div className="t-strong t-num">{t.channels}</div></div>
              <div><div className="t-micro">Peak usage</div><div className="t-strong t-num">{t.usage}%</div></div>
            </div>
            <div className="progress"><div className={`progress-fill ${t.usage > 80 ? "critical" : "good"}`} style={{ width: `${t.usage}%` }} /></div>
          </div>
        ))}
      </div>
    </>
  );
}

function ChannelsSummary() {
  const rows = [
    { id: "voice", name: "Voice (PSTN/SIP)", live: 96, testing: 8, failed: 2, icon: "phone" as const },
    { id: "whatsapp", name: "WhatsApp Business", live: 34, testing: 5, failed: 1, icon: "whatsapp" as const },
    { id: "web", name: "Web widget", live: 41, testing: 11, failed: 3, icon: "monitor" as const },
    { id: "mobile", name: "Mobile SDK", live: 9, testing: 4, failed: 0, icon: "smartphone" as const },
  ];
  return (
    <div className="grid grid-4">
      {rows.map((r) => (
        <div key={r.id} className="card card-pad col gap-12">
          <div className="row gap-12">
            <span className="icon-tile brand"><Icon name={r.icon} size={16} /></span>
            <span className="t-strong" style={{ fontSize: 13.5 }}>{r.name}</span>
          </div>
          <div className="row gap-16">
            <div><div className="t-micro">Live</div><div className="t-strong t-num" style={{ color: "var(--status-good)" }}>{r.live}</div></div>
            <div><div className="t-micro">Testing</div><div className="t-strong t-num">{r.testing}</div></div>
            <div><div className="t-micro">Failed</div><div className="t-strong t-num" style={{ color: r.failed ? "var(--status-critical)" : undefined }}>{r.failed}</div></div>
          </div>
        </div>
      ))}
    </div>
  );
}
