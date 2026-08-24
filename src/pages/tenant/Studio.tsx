import { useNavigate, useParams } from "react-router-dom";
import { useAsync } from "@/hooks/useAsync";
import { getBot } from "@/services/api";
import { Button, CardSkeleton, EmptyState, ErrorState, Health, StatusChip, Tabs } from "@/components/ui";
import { Icon } from "@/components/Icon";
import { useApp } from "@/state/AppContext";
import OverviewTab from "./studio/OverviewTab";
import KnowledgeTab from "./studio/KnowledgeTab";
import PromptsTab from "./studio/PromptsTab";
import VoiceTab from "./studio/VoiceTab";
import TurnDetectionTab from "./studio/TurnDetectionTab";
import IntentsTab from "./studio/IntentsTab";
import ApisTab from "./studio/ApisTab";
import WorkflowsTab from "./studio/WorkflowsTab";
import ChannelsTab from "./studio/ChannelsTab";
import TestingTab from "./studio/TestingTab";
import AnalyticsTab from "./studio/AnalyticsTab";
import PublishTab from "./studio/PublishTab";
import type { Role } from "@/types/domain";

/** Tabs can be permission- or role-gated (UI affordance only; APIs enforce the
 *  same rule). Turn Detection is intentionally Tenant Admin-only. */
const allTabs: { id: string; label: string; perms?: string[]; roles?: Role[] }[] = [
  { id: "overview", label: "Overview" },
  { id: "knowledge", label: "Knowledge" },
  { id: "prompts", label: "Prompts" },
  { id: "voice", label: "Voice" },
  { id: "turn-detection", label: "Turn Detection", roles: ["tenant_admin"] },
  { id: "intents", label: "Intents & Entities", perms: ["manage_intents", "manage_entities", "bots.manage"] },
  { id: "apis", label: "APIs", perms: ["manage_api_connections", "test_api_connections", "integrations.manage"] },
  { id: "workflows", label: "Workflows" },
  { id: "channels", label: "Channels", perms: ["manage_channels"] },
  { id: "testing", label: "Testing" },
  { id: "analytics", label: "Analytics", perms: ["bots.manage"] },
  { id: "publish", label: "Publish", perms: ["bots.publish", "bots.manage"] },
];

export function visibleStudioTabs(
  hasPermission: (code: string) => boolean,
  role: Role = "tenant_admin",
) {
  return allTabs
    .filter((t) => (!t.roles || t.roles.includes(role)) && (!t.perms || t.perms.some(hasPermission)))
    .map(({ id, label }) => ({ id, label }));
}

export default function Studio() {
  const { botId, tab = "overview" } = useParams();
  const navigate = useNavigate();
  const { hasPermission, user } = useApp();
  const botQ = useAsync(() => getBot(botId!), [botId]);

  if (botQ.error) return <ErrorState message={botQ.error} onRetry={botQ.reload} />;
  if (botQ.loading) return <div className="col gap-16"><CardSkeleton rows={2} /><CardSkeleton rows={8} /></div>;
  const bot = botQ.data;
  if (!bot) {
    return <EmptyState icon="bot" title="Bot not found" body="It may have been archived or the link is stale."
      action={<Button onClick={() => navigate("/t/bots")}>Back to My VoiceBots</Button>} />;
  }

  const tabs = visibleStudioTabs(hasPermission, user?.role ?? "tenant_user");
  // A deep link to a hidden tab falls back to Overview — same treatment as an
  // unknown tab segment. The backing APIs reject the calls regardless.
  const activeTab = tabs.some((t) => t.id === tab) ? tab : "overview";
  const canPublish = hasPermission("bots.publish") || hasPermission("bots.manage");

  const readinessDone = bot.readiness.filter((r) => r.done).length;
  const ready = readinessDone === bot.readiness.length;

  return (
    <>
      {/* Studio context bar: identity, status, version, save state, primary actions */}
      <div className="studio-bar">
        <span className="icon-tile brand" style={{ width: 40, height: 40 }}><Icon name="bot" size={18} /></span>
        <div className="grow" style={{ minWidth: 180 }}>
          <div className="row gap-6">
            <span className="t-strong" style={{ fontSize: 15 }}>{bot.name}</span>
            <StatusChip status={bot.status} />
          </div>
          <div className="t-micro mt-4">
            Draft {bot.version}{bot.liveVersion ? <> · live {bot.liveVersion}</> : null} · owner {bot.owner} · {bot.languages.join(", ")}
          </div>
        </div>
        <Health level={bot.health} />
        <span className="row gap-6 t-micro" title="All edits save as draft automatically">
          <Icon name="check-circle" size={13} style={{ color: "var(--status-good)" }} />
          Draft saved
        </span>
        <div className="row gap-6">
          <Button icon="play" onClick={() => navigate(`/t/bots/${bot.id}/testing`)}>Test</Button>
          {canPublish && (
            <Button
              variant="primary"
              icon="rocket"
              title={ready ? "Open Publish Center" : `${bot.readiness.length - readinessDone} readiness checks remaining`}
              onClick={() => navigate(`/t/bots/${bot.id}/publish`)}
            >
              {bot.status === "in_review" ? "In review" : "Publish"}
            </Button>
          )}
        </div>
      </div>

      <div className="studio-tabs-wrap">
        <Tabs tabs={tabs} active={activeTab} onChange={(t) => navigate(`/t/bots/${bot.id}/${t}`)} />
        <div className="studio-panel">
          {activeTab === "overview" && <OverviewTab bot={bot} onUpdated={botQ.reload} />}
          {activeTab === "knowledge" && <KnowledgeTab bot={bot} />}
          {activeTab === "prompts" && <PromptsTab bot={bot} />}
          {activeTab === "voice" && <VoiceTab bot={bot} />}
          {activeTab === "turn-detection" && <TurnDetectionTab />}
          {activeTab === "intents" && <IntentsTab bot={bot} />}
          {activeTab === "apis" && <ApisTab bot={bot} />}
          {activeTab === "workflows" && <WorkflowsTab bot={bot} />}
          {activeTab === "channels" && <ChannelsTab bot={bot} />}
          {activeTab === "testing" && <TestingTab bot={bot} />}
          {activeTab === "analytics" && <AnalyticsTab bot={bot} />}
          {activeTab === "publish" && <PublishTab bot={bot} />}
        </div>
      </div>
    </>
  );
}
