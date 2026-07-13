import { useState } from "react";
import type { Intent, VoiceBot } from "@/types/domain";
import { useAsync } from "@/hooks/useAsync";
import { listEntities, listIntents, simulateAction } from "@/services/api";
import { Button, Callout, Drawer, Progress, StatusChip, Tabs } from "@/components/ui";
import { DataTable } from "@/components/DataTable";
import { Icon } from "@/components/Icon";
import { useApp } from "@/state/AppContext";

export default function IntentsTab({ bot }: { bot: VoiceBot }) {
  const [sub, setSub] = useState("intents");
  const intentsQ = useAsync(() => listIntents(bot.id), [bot.id]);
  const entitiesQ = useAsync(listEntities, []);
  const [open, setOpen] = useState<Intent | null>(null);
  const { toast } = useApp();

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
            <span className="t-sub">Utterance → intent routing. Confidence below threshold triggers the fallback prompt, twice in a row triggers handover.</span>
            <Button variant="primary" size="sm" icon="plus" onClick={() => toast("New intent scaffold created — add at least 5 samples before validation", "info")}>New intent</Button>
          </div>
          <div className="card">
            <DataTable
              loading={intentsQ.loading} error={intentsQ.error} onRetry={intentsQ.reload} rows={intentsQ.data}
              onRowClick={(i) => setOpen(i)}
              empty={{ icon: "target", title: "No intents yet", body: "Define what callers can ask for. Each intent routes to a workflow, a knowledge answer or a human." }}
              columns={[
                { key: "name", header: "Intent", sortValue: (i) => i.name, render: (i) => <div><code className="t-strong" style={{ fontSize: 12.5 }}>{i.name}</code><div className="t-micro">{i.description}</div></div> },
                { key: "samples", header: "Samples", align: "right", sortValue: (i) => i.samples.length, render: (i) => <span className="t-num">{i.samples.length}</span> },
                {
                  key: "conf", header: "Avg confidence (30d)", width: 180, sortValue: (i) => i.avgConfidence30d,
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
                { key: "v", header: "Ver", align: "right", render: (i) => <code>v{i.version}</code> },
              ]}
            />
          </div>
        </>
      )}

      {sub === "entities" && (
        <>
          <Callout tone="warning" title="PII handling">
            Entities marked <b>PII</b> are redacted from stored transcripts and logs by a platform guardrail. Extracted values are used in-call only.
          </Callout>
          <div className="card">
            <DataTable
              loading={entitiesQ.loading} error={entitiesQ.error} onRetry={entitiesQ.reload} rows={entitiesQ.data}
              empty={{ icon: "layers", title: "No entities" }}
              columns={[
                { key: "name", header: "Entity", sortValue: (e) => e.name, render: (e) => <code className="t-strong" style={{ fontSize: 12.5 }}>{e.name}</code> },
                { key: "kind", header: "Kind", render: (e) => <span className="tag" style={{ textTransform: "capitalize" }}>{e.kind}</span> },
                { key: "example", header: "Extraction rule / example", render: (e) => <span className="t-sub" style={{ fontSize: 12.5 }}>{e.example}</span> },
                { key: "pii", header: "PII", render: (e) => e.pii ? <StatusChip status="warning" label="PII — redacted" /> : <span className="t-micro">—</span> },
                { key: "used", header: "Used by", render: (e) => <span className="t-sub" style={{ fontSize: 12 }}>{e.usedBy.join(", ")}</span> },
              ]}
            />
          </div>
        </>
      )}

      <IntentDrawer intent={open} onClose={() => setOpen(null)} />
    </div>
  );
}

function IntentDrawer({ intent, onClose }: { intent: Intent | null; onClose: () => void }) {
  const { toast } = useApp();
  const [testInput, setTestInput] = useState("");
  const [testResult, setTestResult] = useState<{ conf: number; matched: boolean } | null>(null);
  const [busy, setBusy] = useState(false);

  if (!intent) return null;
  const below = intent.avgConfidence30d < intent.confidenceThreshold;

  const runTest = async () => {
    if (!testInput.trim()) return;
    setBusy(true);
    await simulateAction("intent-test");
    const overlap = intent.samples.some((s) => s.split(" ").some((w) => w.length > 3 && testInput.toLowerCase().includes(w.toLowerCase())));
    setTestResult({ conf: overlap ? 0.83 + Math.random() * 0.13 : 0.31 + Math.random() * 0.2, matched: overlap });
    setBusy(false);
  };

  return (
    <Drawer
      open onClose={onClose} wide
      title={<span className="row gap-8"><code>{intent.name}</code><StatusChip status={intent.status} /></span>}
      sub={`${intent.description} · v${intent.version} · routes to ${intent.route}`}
      footer={
        <>
          <Button variant="ghost" icon="history" onClick={() => toast(`Version history: v${intent.version} (current), v${intent.version - 1}, v${intent.version - 2}… restore creates a new draft version`, "info")}>History</Button>
          <Button variant="primary" icon="check" onClick={() => { toast("Intent changes saved to draft"); onClose(); }}>Save changes</Button>
        </>
      }
    >
      <div className="col gap-16">
        {below && (
          <Callout tone="critical" title="Below confidence threshold">
            Average confidence {(intent.avgConfidence30d * 100).toFixed(0)}% is under the {(intent.confidenceThreshold * 100).toFixed(0)}% threshold.
            Add more varied samples — especially phrasings from real escalated calls.
          </Callout>
        )}

        <div>
          <span className="t-label">Training samples ({intent.samples.length})</span>
          <div className="col gap-6 mt-8">
            {intent.samples.map((s, i) => (
              <div key={i} className="row-between card-pad-sm" style={{ background: "var(--surface-2)", borderRadius: 8, fontSize: 12.5 }}>
                “{s}”
                <button className="btn-icon" style={{ width: 24, height: 24 }} aria-label="Remove sample" onClick={() => toast("Sample removed from draft")}>
                  <Icon name="x" size={12} />
                </button>
              </div>
            ))}
            <div className="row gap-8">
              <input className="input" placeholder="Add a sample utterance…" onKeyDown={(e) => {
                if (e.key === "Enter" && (e.target as HTMLInputElement).value.trim()) {
                  toast("Sample added to draft");
                  (e.target as HTMLInputElement).value = "";
                }
              }} aria-label="Add sample" />
            </div>
          </div>
        </div>

        <div className="grid grid-2" style={{ gap: 12 }}>
          <label className="field">
            <span className="field-label">Confidence threshold</span>
            <input className="input" type="number" step={0.01} min={0.3} max={0.95} defaultValue={intent.confidenceThreshold} />
            <span className="field-hint">Below this, the bot asks the fallback prompt.</span>
          </label>
          <label className="field">
            <span className="field-label">Routing target</span>
            <select className="select" defaultValue={intent.route}>
              {["Booking workflow", "Reschedule workflow", "Cancel workflow", "Knowledge answer", "Human handover"].map((r) => <option key={r}>{r}</option>)}
            </select>
          </label>
        </div>

        <div>
          <span className="t-label">Entities extracted</span>
          <div className="row gap-6 mt-8 wrap">
            {intent.entities.length ? intent.entities.map((e) => <code key={e} className="tag">{e}</code>) : <span className="t-micro">none</span>}
          </div>
        </div>

        {/* Quick test */}
        <div className="card-pad-sm" style={{ border: "1px solid var(--hairline)", borderRadius: 10 }}>
          <span className="t-label">Try an utterance</span>
          <div className="row gap-8 mt-8">
            <input className="input" value={testInput} onChange={(e) => setTestInput(e.target.value)}
              placeholder="e.g. can I come in Thursday afternoon?" aria-label="Test utterance"
              onKeyDown={(e) => e.key === "Enter" && runTest()} />
            <Button variant="primary" busy={busy} onClick={runTest}>Test</Button>
          </div>
          {testResult && (
            <div className={`callout ${testResult.matched && testResult.conf >= intent.confidenceThreshold ? "callout-good" : "callout-warning"} mt-8`}>
              <Icon name={testResult.matched ? "check-circle" : "alert"} size={15} />
              <div className="callout-body">
                {testResult.matched && testResult.conf >= intent.confidenceThreshold
                  ? <>Matched <code>{intent.name}</code> at {(testResult.conf * 100).toFixed(0)}% confidence → {intent.route}</>
                  : <>Confidence {(testResult.conf * 100).toFixed(0)}% — below threshold; would trigger fallback prompt. Consider adding this as a sample.</>}
              </div>
            </div>
          )}
        </div>

        <div className="row-between">
          <span className="t-label">Regression tests</span>
          <span className={`t-num t-strong ${intent.testPass < intent.testTotal ? "t-bad" : "t-good"}`}>{intent.testPass}/{intent.testTotal} passing</span>
        </div>
      </div>
    </Drawer>
  );
}
