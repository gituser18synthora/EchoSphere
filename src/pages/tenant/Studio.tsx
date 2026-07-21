import { useNavigate, useParams } from "react-router-dom";
import { useAsync } from "@/hooks/useAsync";
import { getBot } from "@/services/api";
import { Button, CardSkeleton, EmptyState, ErrorState, Health, StatusChip, Tabs } from "@/components/ui";
import { Icon } from "@/components/Icon";
import OverviewTab from "./studio/OverviewTab";
import KnowledgeTab from "./studio/KnowledgeTab";
import PromptsTab from "./studio/PromptsTab";
import VoiceTab from "./studio/VoiceTab";
import IntentsTab from "./studio/IntentsTab";
import ApisTab from "./studio/ApisTab";
import WorkflowsTab from "./studio/WorkflowsTab";
import ChannelsTab from "./studio/ChannelsTab";
import TestingTab from "./studio/TestingTab";
import AnalyticsTab from "./studio/AnalyticsTab";
import PublishTab from "./studio/PublishTab";

const tabs = [
  { id: "overview", label: "Overview" },
  { id: "knowledge", label: "Knowledge" },
  { id: "prompts", label: "Prompts" },
  { id: "voice", label: "Voice" },
  { id: "intents", label: "Intents & Entities" },
  { id: "apis", label: "APIs" },
  { id: "workflows", label: "Workflows" },
  { id: "channels", label: "Channels" },
  { id: "testing", label: "Testing" },
  { id: "analytics", label: "Analytics" },
  { id: "publish", label: "Publish" },
];

export default function Studio() {
  const { botId, tab = "overview" } = useParams();
  const navigate = useNavigate();
  const botQ = useAsync(() => getBot(botId!), [botId]);

  if (botQ.error) return <ErrorState message={botQ.error} onRetry={botQ.reload} />;
  if (botQ.loading) return <div className="col gap-16"><CardSkeleton rows={2} /><CardSkeleton rows={8} /></div>;
  const bot = botQ.data;
  if (!bot) {
    return <EmptyState icon="bot" title="Bot not found" body="It may have been archived or the link is stale."
      action={<Button onClick={() => navigate("/t/bots")}>Back to My VoiceBots</Button>} />;
  }

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
          <Button
            variant="primary"
            icon="rocket"
            title={ready ? "Open Publish Center" : `${bot.readiness.length - readinessDone} readiness checks remaining`}
            onClick={() => navigate(`/t/bots/${bot.id}/publish`)}
          >
            {bot.status === "in_review" ? "In review" : "Publish"}
          </Button>
        </div>
      </div>

      <div className="studio-tabs-wrap">
        <Tabs tabs={tabs} active={tab} onChange={(t) => navigate(`/t/bots/${bot.id}/${t}`)} />
        <div className="studio-panel">
          {tab === "overview" && <OverviewTab bot={bot} onUpdated={botQ.reload} />}
          {tab === "knowledge" && <KnowledgeTab bot={bot} />}
          {tab === "prompts" && <PromptsTab bot={bot} />}
          {tab === "voice" && <VoiceTab bot={bot} />}
          {tab === "intents" && <IntentsTab bot={bot} />}
          {tab === "apis" && <ApisTab bot={bot} />}
          {tab === "workflows" && <WorkflowsTab bot={bot} />}
          {tab === "channels" && <ChannelsTab bot={bot} />}
          {tab === "testing" && <TestingTab bot={bot} />}
          {tab === "analytics" && <AnalyticsTab bot={bot} />}
          {tab === "publish" && <PublishTab bot={bot} />}
        </div>
      </div>
    </>
  );
}
