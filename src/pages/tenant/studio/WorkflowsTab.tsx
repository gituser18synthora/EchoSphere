import { useMemo, useState } from "react";
import type { NodeKind, VoiceBot, WorkflowNode } from "@/types/domain";
import { useAsync } from "@/hooks/useAsync";
import { getWorkflow, simulateAction } from "@/services/api";
import { Button, Callout, CardSkeleton, ErrorState, StatusChip } from "@/components/ui";
import { Icon, type IconName } from "@/components/Icon";
import { useApp } from "@/state/AppContext";

const nodeMeta: Record<NodeKind, { icon: IconName; color: string; label: string }> = {
  start: { icon: "play", color: "var(--series-2)", label: "Start" },
  message: { icon: "message", color: "var(--series-1)", label: "Message" },
  intent: { icon: "target", color: "var(--series-4)", label: "Intent router" },
  condition: { icon: "workflow", color: "var(--series-3)", label: "Condition" },
  api: { icon: "zap", color: "var(--series-8)", label: "API call" },
  knowledge: { icon: "book", color: "var(--series-5)", label: "Knowledge answer" },
  handover: { icon: "headphones", color: "var(--series-7)", label: "Human handover" },
  end: { icon: "check-circle", color: "var(--ink-3)", label: "End" },
};

const NODE_W = 178;
const NODE_H = 54;

export default function WorkflowsTab({ bot }: { bot: VoiceBot }) {
  const q = useAsync(() => getWorkflow(bot.id), [bot.id]);
  const { toast } = useApp();
  const [selected, setSelected] = useState<string | null>(null);
  const [history, setHistory] = useState({ undo: 0, redo: 0 });

  const wf = q.data;
  const selectedNode = useMemo(() => wf?.nodes.find((n) => n.id === selected) ?? null, [wf, selected]);
  const selectedIssues = (wf?.issues ?? []).filter((i) => i.nodeId === selected);

  if (q.error) return <ErrorState message={q.error} onRetry={q.reload} />;
  if (q.loading || !wf) return <CardSkeleton rows={10} />;

  const edgePath = (from: WorkflowNode, to: WorkflowNode) => {
    const x1 = from.x + NODE_W;
    const y1 = from.y + NODE_H / 2;
    const x2 = to.x;
    const y2 = to.y + NODE_H / 2;
    if (to.x < from.x + NODE_W && Math.abs(to.y - from.y) > NODE_H) {
      // vertical connection
      const vx1 = from.x + NODE_W / 2;
      const vy1 = from.y + NODE_H;
      const vx2 = to.x + NODE_W / 2;
      const vy2 = to.y;
      return { d: `M${vx1} ${vy1} C ${vx1} ${vy1 + 34}, ${vx2} ${vy2 - 34}, ${vx2} ${vy2}`, lx: (vx1 + vx2) / 2, ly: (vy1 + vy2) / 2 };
    }
    const mx = (x1 + x2) / 2;
    return { d: `M${x1} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${x2} ${y2}`, lx: mx, ly: (y1 + y2) / 2 - 6 };
  };

  const act = async (msg: string, fn?: () => void) => {
    await simulateAction(msg);
    toast(msg);
    fn?.();
  };

  return (
    <div className="col gap-16">
      <div className="row-between wrap">
        <div className="row gap-8">
          <span className="t-strong" style={{ fontSize: 14 }}>{wf.name}</span>
          <code className="tag">v{wf.version}</code>
          <StatusChip status={wf.status} />
          <span className="t-micro">edited by {wf.updatedBy} · {new Date(wf.updatedAt).toLocaleDateString("en-US", { month: "short", day: "numeric" })}</span>
        </div>
        <div className="row gap-6">
          <Button size="sm" icon="undo" disabled={history.undo === 0} onClick={() => setHistory((h) => ({ undo: h.undo - 1, redo: h.redo + 1 }))}>Undo</Button>
          <Button size="sm" icon="redo" disabled={history.redo === 0} onClick={() => setHistory((h) => ({ undo: h.undo + 1, redo: h.redo - 1 }))}>Redo</Button>
          <Button size="sm" icon="wand" onClick={() => act("Auto-layout applied — nodes arranged by flow depth")}>Auto-layout</Button>
          <Button size="sm" icon="check-circle" onClick={() => act(wf.issues.length ? `Validation found ${wf.issues.length} warnings — see panel below` : "Validation clean")}>Validate</Button>
          <Button size="sm" variant="primary" icon="check" onClick={() => act(`Workflow saved as v${wf.version + 1} draft`)}>Save version</Button>
        </div>
      </div>

      <div className="grid" style={{ gridTemplateColumns: "170px 1fr 280px", gap: 14, alignItems: "start" }}>
        {/* Palette */}
        <div className="card card-pad-sm col gap-4">
          <span className="t-label" style={{ padding: "2px 4px" }}>Node palette</span>
          {(Object.keys(nodeMeta) as NodeKind[]).filter((k) => k !== "start").map((k) => (
            <button
              key={k}
              className="row gap-8"
              style={{ padding: "7px 8px", borderRadius: 8, fontSize: 12.5, fontWeight: 550, border: "1px dashed var(--border)", cursor: "grab" }}
              onClick={() => { setHistory((h) => ({ ...h, undo: h.undo + 1 })); toast(`${nodeMeta[k].label} node added to canvas`); }}
              title={`Add ${nodeMeta[k].label} node`}
            >
              <Icon name={nodeMeta[k].icon} size={13} style={{ color: nodeMeta[k].color }} />
              {nodeMeta[k].label}
            </button>
          ))}
        </div>

        {/* Canvas */}
        <div className="wf-canvas" style={{ height: 570 }} role="application" aria-label="Workflow canvas">
          <div style={{ position: "relative", width: 660, height: 570 }}>
            <svg width={660} height={570} style={{ position: "absolute", inset: 0, pointerEvents: "none" }}>
              <defs>
                <marker id="arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
                  <path d="M0 0 L8 4 L0 8 Z" fill="var(--axis-line)" />
                </marker>
              </defs>
              {wf.edges.map((e) => {
                const from = wf.nodes.find((n) => n.id === e.from)!;
                const to = wf.nodes.find((n) => n.id === e.to)!;
                const p = edgePath(from, to);
                return (
                  <g key={e.id}>
                    <path d={p.d} fill="none" stroke="var(--axis-line)" strokeWidth={1.6} markerEnd="url(#arrow)" />
                    {e.label && (
                      <text x={p.lx} y={p.ly} textAnchor="middle" style={{ fontSize: 10.5, fill: "var(--ink-3)", fontWeight: 600 }}>
                        {e.label}
                      </text>
                    )}
                  </g>
                );
              })}
            </svg>
            {wf.nodes.map((n) => {
              const meta = nodeMeta[n.kind];
              const hasIssue = wf.issues.some((i) => i.nodeId === n.id);
              return (
                <button
                  key={n.id}
                  className={`wf-node${selected === n.id ? " selected" : ""}`}
                  style={{ left: n.x, top: n.y, width: NODE_W, minHeight: NODE_H }}
                  onClick={() => setSelected(n.id === selected ? null : n.id)}
                  aria-pressed={selected === n.id}
                >
                  <span className="wf-node-title">
                    <Icon name={meta.icon} size={13} style={{ color: meta.color }} />
                    {n.label}
                    {hasIssue && <Icon name="alert" size={12} style={{ color: "var(--viz-warning)", marginLeft: "auto" }} />}
                  </span>
                  {n.sub && <span className="wf-node-sub">{n.sub}</span>}
                </button>
              );
            })}
          </div>
        </div>

        {/* Inspector */}
        <div className="card card-pad-sm col gap-10" style={{ minHeight: 300 }}>
          <span className="t-label" style={{ padding: "2px 4px" }}>Inspector</span>
          {!selectedNode && <p className="t-sub" style={{ padding: 4, fontSize: 12.5 }}>Select a node to edit its configuration, or drag from the palette to add one.</p>}
          {selectedNode && (
            <>
              <div className="row gap-8" style={{ padding: "0 4px" }}>
                <Icon name={nodeMeta[selectedNode.kind].icon} size={15} style={{ color: nodeMeta[selectedNode.kind].color }} />
                <span className="t-strong" style={{ fontSize: 13.5 }}>{selectedNode.label}</span>
              </div>
              <label className="field" style={{ padding: "0 4px" }}>
                <span className="field-label">Label</span>
                <input className="input" defaultValue={selectedNode.label} />
              </label>
              {selectedNode.kind === "handover" && (
                <>
                  <label className="field" style={{ padding: "0 4px" }}>
                    <span className="field-label">Agent queue</span>
                    <select className="select" defaultValue="reception">
                      <option value="reception">Front desk / reception</option>
                      <option value="billing">Billing specialists</option>
                      <option value="nurse">On-call nurse line</option>
                    </select>
                  </label>
                  <label className="field" style={{ padding: "0 4px" }}>
                    <span className="field-label">If queue closed</span>
                    <select className="select" defaultValue="">
                      <option value="" disabled>Choose fallback…</option>
                      <option>Offer callback</option>
                      <option>Take a message</option>
                      <option>Play after-hours info</option>
                    </select>
                    <span className="field-error"><Icon name="alert" size={12} />No after-hours fallback set</span>
                  </label>
                </>
              )}
              {selectedNode.kind === "api" && (
                <label className="field" style={{ padding: "0 4px" }}>
                  <span className="field-label">On failure</span>
                  <select className="select" defaultValue="handover">
                    <option value="handover">Route to handover</option>
                    <option value="retry">Retry once, then apologise</option>
                    <option value="skip">Skip and continue</option>
                  </select>
                </label>
              )}
              {selectedIssues.map((i, ix) => (
                <div key={ix} className="callout callout-warning" style={{ padding: "9px 11px", fontSize: 12 }}>
                  <Icon name="alert" size={13} />
                  <div className="callout-body">{i.message}</div>
                </div>
              ))}
              <Button size="sm" variant="danger-ghost" icon="trash" onClick={() => { setSelected(null); setHistory((h) => ({ ...h, undo: h.undo + 1 })); toast("Node removed — reconnect the surrounding edges"); }}>
                Delete node
              </Button>
            </>
          )}
        </div>
      </div>

      {wf.issues.length > 0 && (
        <Callout tone="warning" title={`${wf.issues.length} validation warnings`}>
          {wf.issues.map((i, ix) => {
            const node = wf.nodes.find((n) => n.id === i.nodeId);
            return (
              <div key={ix} className="row gap-6" style={{ marginTop: ix ? 4 : 0 }}>
                <button style={{ fontWeight: 650, textDecoration: "underline" }} onClick={() => setSelected(i.nodeId)}>{node?.label}</button>
                — {i.message}
              </div>
            );
          })}
        </Callout>
      )}
    </div>
  );
}
